import json
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_activity import CustomerTaskActivity
from app.models.customer_contact import CustomerContact
from app.models.hub_agent import HubAgentAction, HubAgentJob
from app.models.hub_mailbox_email import HubMailboxEmail
from app.services.hub_agent import HubAgentError, HubAgentService


def _add_action(db: Session, cipher: SecretCipher, *, action_type: str, input_values: dict[str, str]) -> HubAgentAction:
    job = HubAgentJob(
        created_by_username="hub-admin",
        status="ready",
        encrypted_request_json=cipher.encrypt(json.dumps({"instruction": "Test"})),
        encrypted_plan_json=cipher.encrypt(json.dumps({"summary": "Test", "response": "Test"})),
    )
    db.add(job)
    db.flush()
    action = HubAgentAction(
        job_id=job.id,
        sort_order=0,
        action_type=action_type,
        status="proposed",
        encrypted_payload_json=cipher.encrypt(
            json.dumps({"action_type": action_type, "title": "Test-Aktion", "details": "Test", "input": input_values})
        ),
    )
    db.add(action)
    db.flush()
    return action


def test_hub_agent_executes_a_contact_task_and_draft_only_after_individual_approval():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        customer = Customer(name="Test-Kunde", is_visible=False)
        db.add(customer)
        db.flush()
        contact_action = _add_action(
            db,
            cipher,
            action_type="create_contact",
            input_values={
                "customer_name": "Test-Kunde",
                "salutation": "Herr",
                "first_name": "Max",
                "last_name": "Mustermann",
                "email": "max@example.test",
            },
        )
        task_action = _add_action(
            db,
            cipher,
            action_type="create_task",
            input_values={
                "customer_name": "Test-Kunde",
                "task_name": "Angebot vorbereiten",
                "due_date": "2026-09-11",
                "due_time": "09:00",
                "reminder_channel": "popup",
                "reminder_minutes_before": "0",
            },
        )
        draft_action = _add_action(
            db,
            cipher,
            action_type="create_email_draft",
            input_values={
                "customer_name": "Test-Kunde",
                "recipient_name": "Max Mustermann",
                "recipient_email": "max@example.test",
                "email_subject": "Ihr Angebot",
                "email_html": "<p>Guten Tag Max,</p><p>Ihr Angebot folgt Ende der Woche.</p>",
            },
        )

        service = HubAgentService(db=db, cipher=cipher)
        assert db.scalars(select(CustomerContact)).all() == []
        assert db.scalars(select(CustomerTaskActivity)).all() == []
        assert db.scalars(select(HubMailboxEmail)).all() == []

        contact_view = service.execute_action(action_id=contact_action.id, actor="hub-admin")
        task_view = service.execute_action(action_id=task_action.id, actor="hub-admin")
        draft_view = service.execute_action(action_id=draft_action.id, actor="hub-admin")
        db.commit()

        assert contact_view.status == "completed"
        assert task_view.status == "completed"
        assert draft_view.status == "completed"
        assert contact_view.result_href == f"/contacts/{db.scalars(select(CustomerContact)).one().id}"
        assert db.scalars(select(CustomerContact)).one().customer_id == customer.id
        assert db.scalars(select(CustomerTaskActivity)).one().customer_id == customer.id
        draft = db.scalars(select(HubMailboxEmail)).one()
        assert draft.mailbox_state == "draft"
        assert draft_view.result_href == f"/emails?folder=drafts&selected=unassigned-{draft.id}"


def test_hub_agent_keeps_a_failed_action_for_review_without_creating_data():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        action = _add_action(
            db,
            cipher,
            action_type="create_task",
            input_values={
                "customer_name": "Nicht vorhanden",
                "task_name": "Rückruf",
                "due_date": "2026-09-11",
                "due_time": "09:00",
            },
        )

        view = HubAgentService(db=db, cipher=cipher).execute_action(action_id=action.id, actor="hub-admin")

        assert view.status == "failed"
        assert view.error == "Der Kunde „Nicht vorhanden“ konnte nicht eindeutig zugeordnet werden."
        assert db.scalars(select(CustomerTaskActivity)).all() == []


def test_hub_agent_requests_a_structured_plan_with_only_allowed_actions():
    service = object.__new__(HubAgentService)
    captured = {}

    def fake_response(*, api_key, payload):
        captured["api_key"] = api_key
        captured["payload"] = payload
        return {
            "output": [
                {
                    "type": "function_call",
                    "name": "propose_hub_actions",
                    "arguments": json.dumps(
                        {
                            "summary": "Kontakt und Entwurf vorbereiten",
                            "response": "Ich bereite beide Schritte zur Prüfung vor.",
                            "actions": [
                                {
                                    "action_type": "create_email_draft",
                                    "title": "E-Mail-Entwurf erstellen",
                                    "details": "Der Entwurf bleibt vor dem Versand prüfbar.",
                                    "input": {
                                        "recipient_email": "max@example.test",
                                        "email_subject": "Angebot",
                                        "email_html": "<p>Guten Tag</p>",
                                    },
                                }
                            ],
                        }
                    ),
                }
            ]
        }

    service._create_openai_response = fake_response
    plan = service._create_plan(api_key="test-key", model="test-model", instruction="Bitte einen Entwurf vorbereiten.")

    assert plan["actions"][0]["action_type"] == "create_email_draft"
    assert captured["api_key"] == "test-key"
    assert captured["payload"]["store"] is False
    assert captured["payload"]["tool_choice"] == {"type": "function", "name": "propose_hub_actions"}
    assert captured["payload"]["tools"][0]["parameters"]["properties"]["actions"]["items"]["properties"]["action_type"]["enum"] == [
        "create_contact",
        "create_email_draft",
        "create_task",
    ]


def test_hub_agent_rejects_unapproved_action_types_and_renders_the_controlled_ui():
    try:
        HubAgentService._normalize_action({"action_type": "send_email", "title": "Senden", "details": "", "input": {}})
    except HubAgentError as exc:
        assert "nicht erlaubten" in str(exc)
    else:
        raise AssertionError("Unsupported actions must never be persisted.")

    template = Path("app/templates/agent.html").read_text(encoding="utf-8")
    route = Path("app/api/routes/agent.py").read_text(encoding="utf-8")
    base_template = Path("app/templates/base.html").read_text(encoding="utf-8")

    assert "Jeder Schritt wird erst nach deinem Klick ausgeführt." in template
    assert 'action="/agent/actions/{{ action.id }}/execute"' in template
    assert "E-Mails werden nie automatisch versendet." in template
    assert '@router.post("/actions/{action_id}/execute")' in route
    assert '>Hub-Agent</a>' in base_template
