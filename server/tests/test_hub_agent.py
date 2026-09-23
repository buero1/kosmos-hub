import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from mailbox_fixture_helpers import mailbox_account

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
from app.models.hub_lead import HubLead
from app.models.hub_user import HubUser
from app.models.site import Site
from app.services.hub_agent import HubAgentEmailContext, HubAgentError, HubAgentService
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_email_composition import normalize_reply_html
from app.services.hub_operations import agent_operations


@pytest.mark.parametrize("action_type", ["create_email_draft", "create_email_reply_draft", "emails.drafts.save"])
def test_agent_rejects_legacy_and_ui_only_actions_at_planning_and_execution(action_type):
    raw = {"action_type": action_type, "title": "Test", "details": "Test", "input": {}}
    with pytest.raises(HubAgentError, match="nicht erlaubten"):
        HubAgentService._normalize_action(raw)
    service = object.__new__(HubAgentService)
    with pytest.raises(HubAgentError, match="nicht unterstützt"):
        service._execute_payload(action_type=action_type, payload=raw, actor="hub-admin")


def test_agent_action_enum_is_exactly_the_enabled_registry():
    tool = HubAgentService._proposal_tool_definition()
    actions = tool["parameters"]["properties"]["actions"]["items"]["properties"]["action_type"]["enum"]
    assert actions == sorted(operation.key for operation in agent_operations())


def _add_action(db: Session, cipher: SecretCipher, *, action_type: str, input_values: dict[str, str]) -> HubAgentAction:
    mailbox_account(db, cipher)
    if db.scalar(select(HubUser).where(HubUser.username == "hub-admin")) is None:
        db.add(HubUser(username="hub-admin", password_hash="hash", role="admin", reminder_email="admin@example.test"))
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
            action_type="contacts.create",
            input_values={
                "customer_name": "Test-Kunde",
                "contact_field__salutation": "Herr",
                "contact_field__first_name": "Max",
                "contact_field__last_name": "Mustermann",
                "contact_field__email": "max@example.test",
            },
        )
        task_action = _add_action(
            db,
            cipher,
            action_type="activities.tasks.create",
            input_values={
                "customer_name": "Test-Kunde",
                "name": "Angebot vorbereiten",
                "due_date": "2026-09-11",
                "due_time": "09:00",
                "reminder_channel": "popup",
                "reminder_minutes_before": "0",
            },
        )
        draft_action = _add_action(
            db,
            cipher,
            action_type="emails.drafts.create",
            input_values={
                "customer_id": str(customer.id),
                "recipient_name": "Max Mustermann",
                "recipient_email": "max@example.test",
                "subject": "Ihr Angebot",
                "content": "<p>Guten Tag Max,</p><p>Ihr Angebot folgt Ende der Woche.</p>",
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
            action_type="activities.tasks.create",
            input_values={
                "customer_name": "Nicht vorhanden",
                "name": "Rückruf",
                "due_date": "2026-09-11",
                "due_time": "09:00",
            },
        )

        view = HubAgentService(db=db, cipher=cipher).execute_action(action_id=action.id, actor="hub-admin")

        assert view.status == "failed"
        assert view.error == "Die Verknüpfung wurde nicht gefunden oder ist nicht eindeutig."
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
                                    "action_type": "emails.drafts.create",
                                    "title": "E-Mail-Entwurf erstellen",
                                    "details": "Der Entwurf bleibt vor dem Versand prüfbar.",
                                    "input": {
                                        "recipient_email": "max@example.test",
                                        "subject": "Angebot",
                                        "content": "<p>Guten Tag</p>",
                                    },
                                }
                            ],
                        }
                    ),
                }
            ]
        }

    service._create_openai_response = fake_response
    plan = service._create_plan(api_key="test-key", model="gpt-5.6-sol", instruction="Bitte einen Entwurf vorbereiten.")

    assert plan["actions"][0]["action_type"] == "emails.drafts.create"
    assert captured["api_key"] == "test-key"
    assert captured["payload"]["store"] is False
    assert captured["payload"]["max_output_tokens"] >= 8_000
    assert captured["payload"]["tool_choice"] == "required"
    assert {tool["name"] for tool in captured["payload"]["tools"]} == {"propose_hub_actions", "hub_catalog_search", "hub_catalog_describe", "hub_read"}
    instructions = captured["payload"]["instructions"]
    assert "required=false bedeutet optional" in instructions
    assert "Ein leerer Standard erfüllt kein Pflichtfeld" in instructions
    assert "Wenn Angaben ohne Masken-Standard fehlen" not in instructions
    from app.services.hub_agent_catalog import AgentCatalog
    assert "offer_field__payment_terms" not in instructions
    contract = AgentCatalog().describe(["finance.offers.create"])["definitions"][0]["fields"]
    assert contract["offer_field__payment_terms"]["required"] is False
    assert "default" not in contract["offer_field__payment_terms"]
    assert contract["offer_field__status"]["required"] is True
    assert contract["offer_field__status"]["default"] == "draft"
    assert captured["payload"]["tools"][0]["parameters"]["properties"]["actions"]["items"]["properties"]["action_type"]["enum"] == sorted(
        operation.key for operation in agent_operations()
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
    emails_template = Path("app/templates/emails.html").read_text(encoding="utf-8")

    assert "schwebenden Button unten rechts" in template
    assert 'href="#agent-overview"' in template
    assert 'href="#agent-email-ai-prompts"' in template
    assert "KI-Schnellaktionen" in template
    assert 'action="/agent/email-ai-prompts"' in template
    assert "agent-capability-table" not in template
    assert '@router.post("/email-ai-prompts")' in route
    assert '@router.post("/chat/messages")' in route
    assert '@router.post("/chat/actions/{action_id}/execute")' in route
    assert '@router.post("/chat/{conversation_id}/delete")' in route
    assert 'data-agent-float-open' in base_template
    assert 'data-agent-context-add' in base_template
    assert 'data-agent-float-delete' in base_template
    assert 'data-agent-page-context-type' in base_template
    assert 'async function addPageContext()' in base_template
    assert '>Hub-Agent</a>' in base_template
    assert 'href="#agent-completed-chats"' in template
    assert 'href="#agent-deleted-chats"' in template
    assert "agent_reply_draft" not in emails_template
    assert "function prepareMailboxDraftAction(button)" in emails_template
    assert "openPreparedReplyDraft" not in base_template


def test_hub_agent_persists_context_in_a_user_conversation_and_closes_it():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        customer = Customer(name="Kontext-Kunde", is_visible=True)
        db.add(HubUser(username="hub-admin", password_hash="hash", role="admin"))
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
        assert closed.conversations == ()
        assert [chat.id for chat in service.list_archived_conversations(actor="hub-admin", status="completed")] == [
            started.conversation_id
        ]
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

        active = service.start_conversation(actor="hub-admin")
        after_delete = service.delete_conversation(actor="hub-admin", conversation_id=active.conversation_id)
        assert after_delete.conversation_id != active.conversation_id
        assert db.get(HubAgentConversation, active.conversation_id).status == "deleted"
        assert [chat.id for chat in service.list_archived_conversations(actor="hub-admin", status="deleted")] == [
            active.conversation_id
        ]


def test_new_chat_does_not_resume_or_remove_old_chat_or_its_context():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(HubUser(username="hub-admin", password_hash="hash", role="admin"))
        customer = Customer(name="Old context", is_visible=True)
        db.add(customer)
        db.flush()
        service = HubAgentService(db=db, cipher=SecretCipher("a" * 32))
        old = service.start_conversation(actor="hub-admin")
        service.add_context(actor="hub-admin", conversation_id=old.conversation_id,
                            resource_type="customer", resource_key=str(customer.id))
        fresh = service.start_conversation(actor="hub-admin")
        assert fresh.conversation_id != old.conversation_id
        assert fresh.jobs == () and fresh.contexts == ()
        assert old.conversation_id in {item.id for item in fresh.conversations}
        resumed = service.chat_view(actor="hub-admin", conversation_id=old.conversation_id)
        assert resumed.conversation_id == old.conversation_id
        assert resumed.contexts[0].label == "Kunde: Old context"
    engine.dispose()


def test_hub_agent_builds_a_current_customer_dossier_for_the_chat_context():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        db.add(HubUser(username="hub-admin", password_hash="hash", role="admin"))
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
        assert f"Kunden-ID: {customer.id}" in dossier
        assert "nachladen" in dossier
        assert "Bitte passen Sie die Startseite bis Freitag an." not in dossier
        assert len(dossier) < 1000
        # The full dossier is still available, but no longer eagerly sent on every turn.
        dossier = service._customer_dossier_prompt(customer=customer, actor="hub-admin")
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


def test_hub_agent_uses_the_open_calendar_week_as_dynamic_context():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        customer = Customer(name="Kalender-Kunde", is_visible=True)
        db.add(HubUser(username="hub-admin", password_hash="hash", role="admin"))
        db.add(customer)
        db.flush()
        db.add(
            CustomerCallActivity(
                customer_id=customer.id,
                name="Projekt abstimmen",
                status="planned",
                direction="outbound",
                starts_at=datetime(2026, 9, 8, 10, 0),
                ends_at=datetime(2026, 9, 8, 10, 30),
                duration_minutes=30,
            )
        )
        db.flush()

        service = HubAgentService(db=db, cipher=cipher)
        conversation = service.start_conversation(actor="hub-admin")
        service.add_context(
            actor="hub-admin",
            conversation_id=conversation.conversation_id,
            resource_type="calendar",
            resource_key="2026-09-07",
        )
        active_conversation = service._conversation_for_actor(
            actor="hub-admin",
            conversation_id=conversation.conversation_id,
            create_if_missing=False,
        )
        calendar_context = "\n".join(service._conversation_prompt_contexts(active_conversation, exclude_email_key=""))

        assert "Kalender-Kunde" in calendar_context
        assert "Projekt abstimmen" in calendar_context
        assert "2026-09-08" in calendar_context


def test_hub_agent_discovers_registered_operations_without_a_manual_capability_list():
    tool = HubAgentService._proposal_tool_definition()
    action_types = tool["parameters"]["properties"]["actions"]["items"]["properties"]["action_type"]["enum"]

    assert "finance.offers.create" in action_types
    assert "activities.tasks.create" in action_types
    assert "activities.meetings.create" in action_types
    assert "create_task" not in action_types
    assert "automatic_email_delivery" not in action_types


def test_long_offer_plan_keeps_all_positions_and_materializes_form_defaults():
    positions = {
        f"offer_line__{index}__{key}": value
        for index in range(21)
        for key, value in (("name", f"Position {index + 1}"), ("unit_price", "10"))
    }
    plan = HubAgentService._normalize_plan({
        "summary": "Angebot und Entwurf",
        "response": "Beide Schritte sind vorbereitet.",
        "actions": [
            {"action_type": "finance.offers.create", "title": "Angebot", "details": "21 Positionen",
             "input": {"lead_id": "1791", "offer_field__offer_date": "2026-09-19", **positions}},
            {"action_type": "emails.drafts.create", "title": "E-Mail-Entwurf", "details": "PDF anhängen",
             "input": {"recipient_email": "{{action.1.recipient_email}}",
                       "attachment_ref": "{{action.1.artifact_ref}}", "subject": "Angebot",
                       "content": "<p>Guten Tag</p>"}},
        ],
    })
    offer = plan["actions"][0]["input"]
    assert offer["offer_field__status"] == "draft"
    assert offer["offer_field__valid_until"] == "2026-10-19"
    assert offer["offer_line__20__name"] == "Position 21"
    assert "offer_line__20__unit" not in offer
    assert offer["offer_line__20__tax_rate"] == "19"
    assert plan["actions"][1]["input"]["attachment_ref"] == "{{action.1.artifact_ref}}"


def test_agent_lead_context_is_fresh_and_respects_record_access():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        db.add_all([
            HubUser(username="admin", password_hash="x", role="admin"),
            HubUser(username="sales", password_hash="x", role="sales"),
        ])
        HubAccessControlService(db=db).ensure_defaults()
        lead = HubLead(encrypted_profile_json=cipher.encrypt(json.dumps({
            "schema_version": 1, "source": "hub",
            "fields": {"first_name": "Lena", "last_name": "Leitner", "company": "Leitner Design",
                       "email": "lena@example.test"}, "subforms": {},
        })))
        db.add(lead)
        db.flush()
        service = HubAgentService(db=db, cipher=cipher)
        admin_chat = service.start_conversation(actor="admin")
        service.add_context(
            actor="admin", conversation_id=admin_chat.conversation_id,
            resource_type="lead", resource_key=str(lead.id),
        )
        conversation = db.get(HubAgentConversation, admin_chat.conversation_id)
        prompt = service._conversation_prompt_contexts(conversation, exclude_email_key="")[0]
        assert f"Lead-ID: {lead.id}" in prompt
        assert "lena@example.test" in prompt
        lead.encrypted_profile_json = cipher.encrypt(json.dumps({
            "schema_version": 1, "source": "hub",
            "fields": {"first_name": "Lena", "last_name": "Leitner", "company": "Leitner Design",
                       "email": "neu@example.test"}, "subforms": {},
        }))
        assert "neu@example.test" in service._conversation_prompt_contexts(conversation, exclude_email_key="")[0]
        sales_chat = service.start_conversation(actor="sales")
        try:
            service.add_context(
                actor="sales", conversation_id=sales_chat.conversation_id,
                resource_type="lead", resource_key=str(lead.id),
            )
        except HubAgentError as exc:
            assert "nicht verfügbar" in str(exc)
        else:
            raise AssertionError("Unassigned leads must not be exposed as agent context.")
        db.get(HubUser, 1).is_active = False
        assert service._conversation_prompt_contexts(conversation, exclude_email_key="") == ()


def test_hub_agent_binds_case_actions_to_the_selected_email_context_only():
    raw_plan = {
        "summary": "Fall erstellen",
        "response": "Ich bereite einen Fall vor.",
        "actions": [
            {
                "action_type": "cases.create",
                "title": "Fall aus E-Mail anlegen",
                "details": "Die E-Mail wird mit dem Fall verknüpft.",
                "input": {"case_field__case_reason": "Änderungswunsch", "source_email_key": "linked-7-11"},
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

    assert plan["actions"][0]["input"]["source_email_key"] == "linked-7-11"
    try:
        HubAgentService._normalize_plan(raw_plan)
    except HubAgentError as exc:
        assert "ausgewählten E-Mails" in str(exc)
    else:
        raise AssertionError("Case actions must not be proposed without a selected email.")


def test_hub_agent_reply_plan_uses_the_shared_contract_and_explicit_record_key():
    raw_plan = {
        "summary": "Antwort vorbereiten",
        "response": "Ich öffne den vorhandenen Antworteditor.",
        "actions": [
            {
                "action_type": "emails.drafts.reply",
                "title": "Antwortentwurf erstellen",
                "details": "Empfänger und Betreff werden aus der eingegangenen E-Mail übernommen.",
                "input": {"email_key": "linked-7-11", "content": "<p>Guten Tag,</p><p>vielen Dank.</p>"},
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

    assert plan["actions"][0]["input"] == {
        "content": "<p>Guten Tag,</p><p>vielen Dank.</p>",
        "email_key": "linked-7-11",
    }
    # Keys can also come from authorized read tools, not only pinned contexts.
    assert HubAgentService._normalize_plan(raw_plan)["actions"] == plan["actions"]


def test_hub_agent_normalizes_reply_html_to_hub_typography_and_breaks():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        normalized = normalize_reply_html(db,
            '<p style="font-family: Arial; font-size: 18px">Guten Tag <strong>Frau Beispiel</strong>,</p>'
            '<div style="line-height: 2">vielen Dank für Ihre Nachricht.</div>'
            "<p>Viele Grüße</p>"
        )

        assert normalized == (
            '<span style="font-family: Verdana, Geneva, sans-serif; font-size: 12px; line-height: 1.1">'
            "Guten Tag <strong>Frau Beispiel</strong>,<br><br>vielen Dank für Ihre Nachricht.</span>"
        )
        assert "<p" not in normalized
        assert "<div" not in normalized
        assert "Arial" not in normalized
        assert "Viele Grüße" not in normalized


def test_hub_agent_uses_selected_email_for_case_creation_linking_and_reply_drafts(monkeypatch):
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
            action_type="cases.create",
            input_values={
                "source_email_key": context.key,
                "case_field__case_reason": "Änderungswunsch",
                "case_field__description": "Änderungswunsch aus der E-Mail.",
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
            action_type="cases.link_email",
            input_values={"source_email_key": f"linked-{customer.id}-{second_email.id}", "case_number": case.case_number},
        )
        linked = service.execute_action(action_id=link_action.id, actor="hub-admin")
        assert linked.status == "completed"
        assert len(db.scalars(select(HubCaseEmailLink)).all()) == 2

        monkeypatch.setattr(
            "app.services.hub_agent.CustomerCommunicationService.get_email_reply",
            lambda self, **kwargs: SimpleNamespace(
                email_id=first_email.id,
                recipient_key="contact-1",
                recipient_name="Max Mustermann",
                recipient_email="max@example.test",
                subject="Re: Änderungswunsch",
                content="<br><br><p><strong>Am heute schrieb Max Mustermann:</strong></p><blockquote>Bitte die Startseite ändern.</blockquote>",
            ),
        )
        reply_action = _add_action(
            db,
            cipher,
            action_type="emails.drafts.reply",
            input_values={"email_key": context.key, "content": "<p>Guten Tag,</p><p>Vielen Dank für Ihre Nachricht.</p>"},
        )
        reply = service.execute_action(action_id=reply_action.id, actor="hub-admin")
        draft = db.scalars(select(HubMailboxEmail).where(HubMailboxEmail.mailbox_state == "draft")).one()
        draft_payload = json.loads(cipher.decrypt(draft.encrypted_payload_json))
        assert reply.status == "completed"
        assert reply.result_href == f"/emails?folder=drafts&selected=unassigned-{draft.id}"
        assert draft_payload["recipient_key"] == "contact-1"
        assert draft_payload["content"].startswith(
            '<span style="font-family: Verdana, Geneva, sans-serif; font-size: 12px; line-height: 1.1">'
            "Guten Tag,<br><br>Vielen Dank"
        )
        assert "Bitte die Startseite ändern." in draft_payload["content"]


def test_hub_agent_reply_draft_uses_normal_confirmation_without_sending(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        mailbox_account(db, cipher)
        db.add(HubUser(username="hub-admin", role="admin", password_hash="x"))
        inbound = HubMailboxEmail(
            source="mittwald-imap",
            direction="inbound",
            is_unread=True,
            mailbox_state="active",
            fingerprint="a" * 64,
            encrypted_payload_json=cipher.encrypt(
                json.dumps(
                    {
                        "subject": "Rückfrage zur Website",
                        "from": {"name": "Frau Beispiel", "email": "frau@example.test"},
                        "content": "Bitte geben Sie kurz Bescheid.",
                    }
                )
            ),
            received_at=datetime(2026, 9, 10, 9, 0),
        )
        db.add(inbound)
        db.flush()

        service = HubAgentService(db=db, cipher=cipher)
        conversation = service.start_conversation(actor="hub-admin")
        service.add_context(
            actor="hub-admin",
            conversation_id=conversation.conversation_id,
            resource_type="email",
            resource_key=f"unassigned-{inbound.id}",
        )
        monkeypatch.setattr(
            service.provider_service,
            "get_enabled_openai_api_key",
            lambda: (SimpleNamespace(model="test-model"), "test-key"),
        )
        monkeypatch.setattr(service.provider_service, "record_request_success", lambda config: None)
        monkeypatch.setattr(
            service,
            "_create_plan",
            lambda **kwargs: {
                "summary": "Antwort formulieren",
                "response": "Ich habe einen Antwortentwurf vorbereitet.",
                "actions": [
                    {
                        "action_type": "emails.drafts.reply",
                        "title": "Antwortentwurf erstellen",
                        "details": "Der Entwurf wird nicht versendet.",
                        "input": {
                            "email_key": f"unassigned-{inbound.id}",
                            "content": "<p>Guten Tag Frau Beispiel,</p><p>vielen Dank für Ihre Nachricht.</p>",
                        },
                    }
                ],
            },
        )

        job = service.plan(
            instruction="Bitte freundlich antworten.",
            actor="hub-admin",
            conversation_id=conversation.conversation_id,
        )

        assert job.status == "ready"
        assert job.actions[0].status == "proposed"
        assert db.scalars(select(HubMailboxEmail).where(HubMailboxEmail.mailbox_state == "draft")).all() == []
        result = service.execute_action(action_id=job.actions[0].id, actor="hub-admin")
        assert result.status == "completed"
        draft = db.scalars(select(HubMailboxEmail).where(HubMailboxEmail.mailbox_state == "draft")).one()
        assert result.result_href == f"/emails?folder=drafts&selected=unassigned-{draft.id}"
        draft_payload = json.loads(cipher.decrypt(draft.encrypted_payload_json))
        assert draft_payload["recipient_email"] == "frau@example.test"
        assert draft_payload["subject"] == "Re: Rückfrage zur Website"
        assert draft_payload["content"].startswith(
            '<span style="font-family: Verdana, Geneva, sans-serif; font-size: 12px; line-height: 1.1">'
            "Guten Tag Frau Beispiel,<br><br>vielen Dank"
        )
        assert "Am 2026-09-10T09:00:00 schrieb" in draft_payload["content"]


def test_hub_agent_manages_tasks_calls_and_customer_notes(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    noted_customers = []

    def fake_create_note(self, **kwargs):
        noted_customers.append(kwargs)
        return SimpleNamespace(success=True, message="Notiz gespeichert", note_id=1)

    monkeypatch.setattr("app.services.hub_agent.CustomerCommunicationService.create_note", fake_create_note)

    with Session(engine) as db:
        customer = Customer(name="Aktivitäts-Kunde", is_visible=True)
        db.add(customer)
        db.flush()
        service = HubAgentService(db=db, cipher=cipher)

        create_task = _add_action(
            db,
            cipher,
            action_type="activities.tasks.create",
            input_values={
                "customer_name": customer.name,
                "name": "Angebot prüfen",
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
            action_type="activities.tasks.complete",
            input_values={"customer_name": customer.name, "target_name": "Angebot prüfen"},
        )
        assert service.execute_action(action_id=complete_task.id, actor="hub-admin").status == "completed"
        assert db.scalars(select(CustomerTaskActivity)).one().status == "completed"

        adjustable_task = _add_action(
            db,
            cipher,
            action_type="activities.tasks.create",
            input_values={
                "customer_name": customer.name,
                "name": "Termin ändern",
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
            action_type="activities.tasks.update",
            input_values={
                "customer_name": customer.name,
                "target_name": "Termin ändern",
                "name": "Neuer Termin",
                "due_time": "11:00",
            },
        )
        assert service.execute_action(action_id=update_task.id, actor="hub-admin").status == "completed"
        assert db.scalars(select(CustomerTaskActivity).where(CustomerTaskActivity.name == "Neuer Termin")).one().due_at.hour == 9
        delete_task = _add_action(
            db,
            cipher,
            action_type="activities.tasks.delete",
            input_values={"customer_name": customer.name, "target_name": "Neuer Termin"},
        )
        assert service.execute_action(action_id=delete_task.id, actor="hub-admin").status == "completed"

        create_call = _add_action(
            db,
            cipher,
            action_type="activities.calls.create",
            input_values={
                "customer_name": customer.name,
                "name": "Rückruf",
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
            action_type="activities.calls.update",
            input_values={
                "customer_name": customer.name,
                "target_name": "Rückruf",
                "start_time": "11:00",
            },
        )
        assert service.execute_action(action_id=update_call.id, actor="hub-admin").status == "completed"
        assert db.scalars(select(CustomerCallActivity)).one().starts_at.hour == 9

        complete_call = _add_action(
            db,
            cipher,
            action_type="activities.calls.complete",
            input_values={"customer_name": customer.name, "target_name": "Rückruf"},
        )
        assert service.execute_action(action_id=complete_call.id, actor="hub-admin").status == "completed"
        assert db.scalars(select(CustomerCallActivity)).one().status == "completed"

        delete_call = _add_action(
            db,
            cipher,
            action_type="activities.calls.delete",
            input_values={"customer_name": customer.name, "target_name": "Rückruf"},
        )
        assert service.execute_action(action_id=delete_call.id, actor="hub-admin").status == "completed"
        assert db.scalars(select(CustomerCallActivity)).all() == []

        note_action = _add_action(
            db,
            cipher,
            action_type="customers.notes.create",
            input_values={"customer_name": customer.name, "title": "Rückruf", "content": "Rückruf wurde erledigt."},
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
