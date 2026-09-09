import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_activity import CustomerCallActivity, CustomerTaskActivity
from app.models.customer_communication import CustomerZohoEmail, CustomerZohoNote
from app.models.customer_contact import CustomerContact
from app.models.hub_agent import HubAgentAction, HubAgentConversation, HubAgentConversationContext, HubAgentJob
from app.models.hub_case import HubCase
from app.models.hub_case_email_link import HubCaseEmailLink
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.site import Site
from app.services.hub_agent import HubAgentEmailContext, HubAgentError, HubAgentService


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
    assert captured["payload"]["tools"][0]["parameters"]["properties"]["actions"]["items"]["properties"]["action_type"]["enum"] == sorted(
        {
            "complete_call",
            "complete_task",
            "create_case_from_email",
            "create_contact",
            "create_customer_note",
            "create_email_draft",
            "create_task",
            "delete_call",
            "delete_task",
            "link_email_to_case",
            "schedule_call",
            "update_call",
            "update_task",
        }
    )


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

    assert "schwebenden Button unten rechts" in template
    assert 'href="#agent-capabilities"' in template
    assert "agent-capability-status-{{ capability.status }}" in template
    assert '@router.post("/chat/messages")' in route
    assert '@router.post("/chat/actions/{action_id}/execute")' in route
    assert 'data-agent-float-open' in base_template
    assert 'data-agent-context-add' in base_template
    assert '>Hub-Agent</a>' in base_template


def test_hub_agent_persists_context_in_a_user_conversation_and_closes_it():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        customer = Customer(name="Kontext-Kunde", is_visible=True)
        db.add(customer)
        db.flush()
        service = HubAgentService(db=db, cipher=cipher)

        started = service.start_conversation(actor="hub-admin")
        assert started.title == "Neue Unterhaltung"

        chat = service.add_context(
            actor="hub-admin",
            conversation_id=started.conversation_id,
            resource_type="customer",
            resource_key=str(customer.id),
        )
        assert chat.conversation_id == started.conversation_id
        assert [context.label for context in chat.contexts] == ["Kunde: Kontext-Kunde"]
        assert db.scalars(select(HubAgentConversation)).one().created_by_username == "hub-admin"
        assert db.scalars(select(HubAgentConversationContext)).one().resource_key == str(customer.id)

        closed = service.close_conversation(actor="hub-admin", conversation_id=started.conversation_id)
        assert closed.status == "completed"
        try:
            service.add_context(
                actor="hub-admin",
                conversation_id=started.conversation_id,
                resource_type="customer",
                resource_key=str(customer.id),
            )
        except HubAgentError as exc:
            assert "abgeschlossen" in str(exc)
        else:
            raise AssertionError("Completed Hub-Agent conversations must be immutable.")


def test_hub_agent_builds_a_current_customer_dossier_for_the_chat_context():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        customer = Customer(
            name="Wissen-Kunde",
            is_visible=True,
            website_domain="wissen-kunde.example",
            encrypted_profile_json=cipher.encrypt(
                json.dumps(
                    {
                        "fields": {
                            "Telefon": "089 123456",
                            "Rechnungsadresse - Stadt": "München",
                        }
                    }
                )
            ),
        )
        db.add(customer)
        db.flush()
        db.add_all(
            [
                CustomerContact(
                    customer_id=customer.id,
                    encrypted_profile_json=cipher.encrypt(
                        json.dumps(
                            {
                                "fields": {
                                    "Name": "Max Mustermann",
                                    "E-Mail": "max@wissen-kunde.example",
                                    "Mobil": "0170 1234567",
                                }
                            }
                        )
                    ),
                ),
                Site(
                    uuid="00000000-0000-0000-0000-000000000001",
                    customer_id=customer.id,
                    domain="wissen-kunde.example",
                    home_url="https://wissen-kunde.example",
                    site_url="https://wissen-kunde.example/wp-admin",
                    status="verified",
                ),
                HubCase(
                    customer_id=customer.id,
                    case_number="FALL-000001",
                    encrypted_fields_json=cipher.encrypt(
                        json.dumps(
                            {
                                "status": "Neu",
                                "case_reason": "Änderungswunsch",
                                "case_origin": "E-Mail",
                                "created_time": "2026-09-09T09:30",
                                "description": "Startseite anpassen",
                            }
                        )
                    ),
                ),
                CustomerTaskActivity(
                    customer_id=customer.id,
                    name="Änderungswunsch prüfen",
                    status="planned",
                    due_at=datetime(2026, 9, 10, 9, 0),
                    reminder_channel="popup",
                    reminder_minutes_before=15,
                    description="Mit dem Kunden abstimmen",
                ),
                CustomerZohoNote(
                    customer_id=customer.id,
                    source="hub",
                    encrypted_payload_json=cipher.encrypt(
                        json.dumps({"title": "Telefonat", "content": "Der Kunde erwartet den Rückruf am Freitag."})
                    ),
                ),
                CustomerZohoEmail(
                    customer_id=customer.id,
                    source="hub",
                    direction="inbound",
                    encrypted_payload_json=cipher.encrypt(
                        json.dumps(
                            {
                                "subject": "Bitte um Änderung",
                                "from": {"name": "Max Mustermann", "email": "max@wissen-kunde.example"},
                                "to": [{"email": "info@kosmos-medien.de"}],
                                "content": "<p>Bitte passen Sie die Startseite bis Freitag an.</p>",
                            }
                        )
                    ),
                    encrypted_header_json="",
                ),
            ]
        )
        db.flush()

        service = HubAgentService(db=db, cipher=cipher)
        conversation = service.start_conversation(actor="hub-admin")
        service.add_context(
            actor="hub-admin",
            conversation_id=conversation.conversation_id,
            resource_type="customer",
            resource_key=str(customer.id),
        )
        active_conversation = service._conversation_for_actor(
            actor="hub-admin",
            conversation_id=conversation.conversation_id,
            create_if_missing=False,
        )
        dossier = "\n".join(service._conversation_prompt_contexts(active_conversation, exclude_email_key=""))

        assert "Telefon: 089 123456" in dossier
        assert "München" in dossier
        assert "Max Mustermann" in dossier
        assert "max@wissen-kunde.example" in dossier
        assert "wissen-kunde.example/wp-admin" in dossier
        assert "FALL-000001" in dossier
        assert "Änderungswunsch prüfen" in dossier
        assert "Der Kunde erwartet den Rückruf am Freitag." in dossier
        assert "Bitte um Änderung" in dossier
        assert "Bitte passen Sie die Startseite bis Freitag an." in dossier


def test_hub_agent_exposes_current_and_planned_capabilities_in_one_catalog():
    capabilities = {capability.key: capability for capability in HubAgentService.capabilities()}

    assert capabilities["create_contact"].status == "available"
    assert capabilities["create_task"].status == "available"
    assert capabilities["create_email_draft"].status == "available"
    assert capabilities["email_context"].status == "available"
    assert capabilities["customer_dossier"].status == "available"
    assert capabilities["case_management"].status == "available"
    assert capabilities["customer_notes"].status == "available"
    assert capabilities["calendar_management"].status == "available"
    assert capabilities["automatic_email_delivery"].status == "disabled"


def test_hub_agent_binds_case_actions_to_the_selected_email_context_only():
    raw_plan = {
        "summary": "Fall erstellen",
        "response": "Ich bereite einen Fall vor.",
        "actions": [
            {
                "action_type": "create_case_from_email",
                "title": "Fall aus E-Mail anlegen",
                "details": "Die E-Mail wird mit dem Fall verknüpft.",
                "input": {"case_reason": "Änderungswunsch"},
            }
        ],
    }
    context = HubAgentEmailContext(
        key="linked-7-11",
        subject="Änderungswunsch",
        sender="",
        recipients="",
        customer_id=7,
        customer_name="Kontext-Kunde",
        body_text="",
        attachment_names=(),
    )

    plan = HubAgentService._normalize_plan(raw_plan, email_context=context)

    assert plan["actions"][0]["input"]["email_key"] == "linked-7-11"
    try:
        HubAgentService._normalize_plan(raw_plan)
    except HubAgentError as exc:
        assert "E-Mail als Kontext" in str(exc)
    else:
        raise AssertionError("Case actions must not be proposed without a selected email.")


def test_hub_agent_uses_selected_email_for_case_creation_and_linking():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        customer = Customer(name="Fall-Kunde", is_visible=True)
        db.add(customer)
        db.flush()
        first_email = CustomerZohoEmail(
            customer_id=customer.id,
            source="hub",
            direction="inbound",
            encrypted_payload_json=cipher.encrypt(
                json.dumps(
                    {
                        "subject": "Änderungswunsch",
                        "from": {"name": "Max Mustermann", "email": "max@example.test"},
                        "to": [{"email": "info@example.test"}],
                        "content": "<p>Bitte die Startseite ändern.</p>",
                        "attachments": [{"id": "attachment-1", "name": "wunsch.pdf"}],
                    }
                )
            ),
            encrypted_header_json="",
        )
        second_email = CustomerZohoEmail(
            customer_id=customer.id,
            source="hub",
            direction="inbound",
            encrypted_payload_json=cipher.encrypt(json.dumps({"subject": "Nachtrag", "content": "Weitere Infos"})),
            encrypted_header_json="",
        )
        db.add_all([first_email, second_email])
        db.flush()

        service = HubAgentService(db=db, cipher=cipher)
        context = service.get_email_context(email_key=f"linked-{customer.id}-{first_email.id}")
        assert context.customer_name == "Fall-Kunde"
        assert context.sender == "Max Mustermann <max@example.test>"
        assert context.attachment_names == ("wunsch.pdf",)
        assert context.body_text == "Bitte die Startseite ändern."

        create_action = _add_action(
            db,
            cipher,
            action_type="create_case_from_email",
            input_values={
                "email_key": context.key,
                "case_reason": "Änderungswunsch",
                "case_description": "Änderungswunsch aus der E-Mail.",
            },
        )
        created = service.execute_action(action_id=create_action.id, actor="hub-admin")
        case = db.scalars(select(HubCase)).one()
        assert created.status == "completed"
        assert created.result_href == f"/cases/{case.id}"
        assert case.customer_id == customer.id
        assert db.scalars(select(HubCaseEmailLink)).one().customer_email_id == first_email.id

        link_action = _add_action(
            db,
            cipher,
            action_type="link_email_to_case",
            input_values={"email_key": f"linked-{customer.id}-{second_email.id}", "case_number": case.case_number},
        )
        linked = service.execute_action(action_id=link_action.id, actor="hub-admin")
        assert linked.status == "completed"
        assert len(db.scalars(select(HubCaseEmailLink)).all()) == 2


def test_hub_agent_manages_tasks_calls_and_customer_notes(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    noted_customers = []

    def fake_create_note(self, **kwargs):
        noted_customers.append(kwargs)
        return object()

    monkeypatch.setattr("app.services.hub_agent.CustomerCommunicationService.create_note", fake_create_note)

    with Session(engine) as db:
        customer = Customer(name="Aktivitäts-Kunde", is_visible=True)
        db.add(customer)
        db.flush()
        service = HubAgentService(db=db, cipher=cipher)

        create_task = _add_action(
            db,
            cipher,
            action_type="create_task",
            input_values={
                "customer_name": customer.name,
                "task_name": "Angebot prüfen",
                "due_date": "2026-09-11",
                "due_time": "09:00",
                "reminder_channel": "popup",
                "reminder_minutes_before": "0",
            },
        )
        assert service.execute_action(action_id=create_task.id, actor="hub-admin").status == "completed"

        complete_task = _add_action(
            db,
            cipher,
            action_type="complete_task",
            input_values={"customer_name": customer.name, "target_task_name": "Angebot prüfen"},
        )
        assert service.execute_action(action_id=complete_task.id, actor="hub-admin").status == "completed"
        assert db.scalars(select(CustomerTaskActivity)).one().status == "completed"

        adjustable_task = _add_action(
            db,
            cipher,
            action_type="create_task",
            input_values={
                "customer_name": customer.name,
                "task_name": "Termin ändern",
                "due_date": "2026-09-12",
                "due_time": "09:00",
                "reminder_channel": "popup",
                "reminder_minutes_before": "0",
            },
        )
        assert service.execute_action(action_id=adjustable_task.id, actor="hub-admin").status == "completed"
        update_task = _add_action(
            db,
            cipher,
            action_type="update_task",
            input_values={
                "customer_name": customer.name,
                "target_task_name": "Termin ändern",
                "task_name": "Neuer Termin",
                "due_time": "11:00",
            },
        )
        assert service.execute_action(action_id=update_task.id, actor="hub-admin").status == "completed"
        assert db.scalars(select(CustomerTaskActivity).where(CustomerTaskActivity.name == "Neuer Termin")).one().due_at.hour == 9
        delete_task = _add_action(
            db,
            cipher,
            action_type="delete_task",
            input_values={"customer_name": customer.name, "target_task_name": "Neuer Termin"},
        )
        assert service.execute_action(action_id=delete_task.id, actor="hub-admin").status == "completed"

        create_call = _add_action(
            db,
            cipher,
            action_type="schedule_call",
            input_values={
                "customer_name": customer.name,
                "call_name": "Rückruf",
                "start_date": "2026-09-12",
                "start_time": "10:00",
                "duration_minutes": "30",
                "reminder_channels": "popup",
                "reminder_minutes_before": "15",
            },
        )
        assert service.execute_action(action_id=create_call.id, actor="hub-admin").status == "completed"

        update_call = _add_action(
            db,
            cipher,
            action_type="update_call",
            input_values={
                "customer_name": customer.name,
                "target_call_name": "Rückruf",
                "start_time": "11:00",
            },
        )
        assert service.execute_action(action_id=update_call.id, actor="hub-admin").status == "completed"
        assert db.scalars(select(CustomerCallActivity)).one().starts_at.hour == 9

        complete_call = _add_action(
            db,
            cipher,
            action_type="complete_call",
            input_values={"customer_name": customer.name, "target_call_name": "Rückruf"},
        )
        assert service.execute_action(action_id=complete_call.id, actor="hub-admin").status == "completed"
        assert db.scalars(select(CustomerCallActivity)).one().status == "completed"

        delete_call = _add_action(
            db,
            cipher,
            action_type="delete_call",
            input_values={"customer_name": customer.name, "target_call_name": "Rückruf"},
        )
        assert service.execute_action(action_id=delete_call.id, actor="hub-admin").status == "completed"
        assert db.scalars(select(CustomerCallActivity)).all() == []

        note_action = _add_action(
            db,
            cipher,
            action_type="create_customer_note",
            input_values={"customer_name": customer.name, "note_title": "Rückruf", "note_content": "Rückruf wurde erledigt."},
        )
        assert service.execute_action(action_id=note_action.id, actor="hub-admin").status == "completed"
        assert noted_customers == [
            {
                "customer_id": customer.id,
                "actor": "hub-admin",
                "title": "Rückruf",
                "content": "Rückruf wurde erledigt.",
            }
        ]
