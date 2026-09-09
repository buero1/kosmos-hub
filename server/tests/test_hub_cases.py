import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_activity import CustomerTaskActivity
from app.models.customer_activity_reminder_notification import CustomerActivityReminderNotification
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_case import HubCase
from app.models.hub_case_email_link import HubCaseEmailLink
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.hub_user import HubUser
from app.services.hub_cases import CASE_FIELDS_LAYOUT_KEY, HubCaseError, HubCaseService
from app.services.hub_workflows import CASE_OPEN_REMINDER_WORKFLOW_KEY, HubWorkflowService
from app.services.module_layouts import ModuleLayoutService


def _service(db: Session) -> HubCaseService:
    return HubCaseService(db=db, cipher=SecretCipher("a" * 32))


def _submitted_values(**overrides: str) -> dict[str, str]:
    values = {
        "case_field__status": "Neu",
        "case_field__case_reason": "Änderungswunsch",
        "case_field__case_origin": "E-Mail",
        "case_field__created_time": "2026-09-09T09:00",
        "case_field__description": "Der beschriebene Fall.",
        "case_field__duration_minutes": "45",
        "case_field__billed_amount_net": "250",
    }
    values.update(overrides)
    return values


def test_hub_case_is_numbered_linked_and_stored_encrypted():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Fall Kunde", is_visible=True)
        db.add(customer)
        db.flush()

        case = _service(db).create_case(customer_id=customer.id, submitted_values=_submitted_values())
        db.commit()

        assert case.case_number == "FALL-000001"
        assert case.customer_id == customer.id
        assert "Der beschriebene Fall." not in case.encrypted_fields_json
        stored_values = json.loads(SecretCipher("a" * 32).decrypt(case.encrypted_fields_json))
        assert stored_values["status"] == "Neu"
        assert stored_values["duration_minutes"] == "45"

        detail = _service(db).get_detail(case_id=case.id)
        assert detail is not None
        assert detail.case_number == "FALL-000001"
        assert [(field.label, field.value) for field in detail.fields if field.key == "customer_name"] == [
            ("Kunde-Name", "Fall Kunde")
        ]
        assert [(field.label, field.value) for field in detail.fields if field.key == "created_time"] == [
            ("Zeitpunkt der Erstellung", "09.09.2026 09:00")
        ]


def test_hub_case_validates_required_choices_and_integer_fields():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = _service(db)
        with pytest.raises(HubCaseError, match="Fall Ursprung ist erforderlich"):
            service.create_case(customer_id=None, submitted_values=_submitted_values(**{"case_field__case_origin": "-None-"}))
        with pytest.raises(HubCaseError, match="Ganzzahl"):
            service.create_case(customer_id=None, submitted_values=_submitted_values(**{"case_field__duration_minutes": "eine Stunde"}))


def test_hub_case_update_replaces_fields_and_customer_link():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        first_customer = Customer(name="Erster Kunde", is_visible=True)
        second_customer = Customer(name="Zweiter Kunde", is_visible=True)
        db.add_all([first_customer, second_customer])
        db.flush()
        service = _service(db)
        case = service.create_case(customer_id=first_customer.id, submitted_values=_submitted_values())
        db.commit()

        updated = service.update_case(
            case_id=case.id,
            customer_id=second_customer.id,
            submitted_values=_submitted_values(**{"case_field__status": "Abgeschlossen", "case_field__duration_minutes": "0"}),
        )
        db.commit()

        assert updated.customer_id == second_customer.id
        assert _service(db).list_cases()[0].status == "Abgeschlossen"
        assert db.get(HubCase, case.id) is not None


def test_open_case_creates_a_popup_task_and_completion_removes_it():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Workflow Kunde", is_visible=True)
        user = HubUser(username="operator", password_hash="hashed", role="admin")
        db.add_all([customer, user])
        db.flush()
        service = _service(db)
        case = service.create_case(
            customer_id=customer.id,
            submitted_values=_submitted_values(),
            actor_username="operator",
        )

        task = db.scalar(select(CustomerTaskActivity).where(CustomerTaskActivity.case_id == case.id))
        assert task is not None
        assert task.name == "Ein offener Fall vom 09.09.2026 09:00"
        assert task.created_by_username == "operator"
        assert task.reminder_channel == "popup"
        assert task.reminder_minutes_before == 0
        assert task.due_at == datetime(2026, 9, 10, 3, 0)
        assert any(
            workflow.workflow_key == CASE_OPEN_REMINDER_WORKFLOW_KEY
            for workflow in HubWorkflowService(db=db).list_workflows()
        )

        db.add(
            CustomerActivityReminderNotification(
                user_id=user.id,
                customer_id=customer.id,
                activity_kind="task",
                activity_id=task.id,
                reminder_key="primary",
                remind_at=task.due_at,
            )
        )
        db.flush()
        service.update_case(
            case_id=case.id,
            customer_id=customer.id,
            submitted_values=_submitted_values(**{"case_field__status": "Abgeschlossen"}),
        )

        assert db.scalar(select(CustomerTaskActivity).where(CustomerTaskActivity.case_id == case.id)) is None
        assert db.scalar(select(CustomerActivityReminderNotification)) is None


def test_case_delete_removes_its_workflow_task():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Löschkunde", is_visible=True)
        db.add(customer)
        db.flush()
        case = _service(db).create_case(customer_id=customer.id, submitted_values=_submitted_values())
        assert db.scalar(select(CustomerTaskActivity).where(CustomerTaskActivity.case_id == case.id)) is not None

        _service(db).delete_case(case_id=case.id)

        assert db.scalar(select(CustomerTaskActivity).where(CustomerTaskActivity.case_id == case.id)) is None


def test_hub_case_detail_uses_the_saved_global_field_layout():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        admin = HubUser(username="operator", password_hash="hashed", role="admin")
        db.add(admin)
        db.flush()
        case = _service(db).create_case(customer_id=None, submitted_values=_submitted_values())
        db.commit()

        original_detail = _service(db).get_detail(case_id=case.id)
        assert original_detail is not None
        original_keys = tuple(field.key for field in original_detail.fields)
        reordered_keys = ("description",) + tuple(key for key in original_keys if key != "description")
        ModuleLayoutService(db=db).configure(
            actor=admin,
            layout_key=CASE_FIELDS_LAYOUT_KEY,
            item_order_json=json.dumps(reordered_keys),
            allowed_keys=original_keys,
        )
        db.commit()

        detail = _service(db).get_detail(case_id=case.id)

        assert detail is not None
        assert tuple(field.key for field in detail.fields) == reordered_keys


def test_hub_case_delete_removes_only_the_hub_case():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        case = _service(db).create_case(customer_id=None, submitted_values=_submitted_values())
        case_id = case.id
        db.commit()

        deleted = _service(db).delete_case(case_id=case_id)
        db.commit()

        assert deleted.id == case_id
        assert db.get(HubCase, case_id) is None


def test_hub_case_links_customer_and_mailbox_emails_without_duplicates():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        customer = Customer(name="E-Mail Kunde", is_visible=True)
        customer_email = CustomerZohoEmail(
            customer=customer,
            source="zoho",
            direction="inbound",
            is_unread=True,
            encrypted_payload_json=cipher.encrypt(
                json.dumps({"subject": "Anfrage zum Fall", "from": {"email": "kunde@example.de"}, "content": "<p>Bitte helfen.</p>"})
            ),
            zoho_sent_at=datetime(2026, 9, 9, 10, 0, tzinfo=UTC),
        )
        mailbox_email = HubMailboxEmail(
            source="mittwald-imap",
            direction="outbound",
            is_unread=False,
            fingerprint="m" * 64,
            encrypted_payload_json=cipher.encrypt(
                json.dumps({"subject": "Rückfrage", "sender": "team@example.de", "content": "<p>Bitte um Rückruf.</p>"})
            ),
            received_at=datetime(2026, 9, 9, 11, 0, tzinfo=UTC),
        )
        db.add_all([customer, customer_email, mailbox_email])
        db.flush()

        service = _service(db)
        case = service.create_case(customer_id=customer.id, submitted_values=_submitted_values())
        first_link = service.link_email(
            case_id=case.id,
            source_email_key=f"linked-{customer.id}-{customer_email.id}",
        )
        duplicate_link = service.link_email(
            case_id=case.id,
            source_email_key=f"linked-{customer.id}-{customer_email.id}",
        )
        second_link = service.link_email(case_id=case.id, source_email_key=f"unassigned-{mailbox_email.id}")
        db.commit()

        assert duplicate_link.id == first_link.id
        assert db.query(HubCaseEmailLink).count() == 2
        detail = service.get_detail(case_id=case.id)
        assert detail is not None
        assert [email.subject for email in detail.linked_emails] == ["Rückfrage", "Anfrage zum Fall"]
        assert detail.linked_emails[1].preview_html is not None
        assert "Bitte helfen." in detail.linked_emails[1].preview_html
        assert detail.linked_emails[0].mailbox_folder == "sent"

        service.unlink_email(case_id=case.id, link_id=second_link.id)
        db.commit()
        assert [email.subject for email in service.get_detail(case_id=case.id).linked_emails] == ["Anfrage zum Fall"]


def test_hub_case_returns_the_case_for_one_email_and_rejects_a_second_case_link():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        mailbox_email = HubMailboxEmail(
            source="mittwald-imap",
            direction="inbound",
            is_unread=True,
            fingerprint="m" * 64,
            encrypted_payload_json=cipher.encrypt(json.dumps({"subject": "Neue Anfrage"})),
            received_at=datetime(2026, 9, 9, 10, 0, tzinfo=UTC),
        )
        db.add(mailbox_email)
        db.flush()
        service = _service(db)
        first_case = service.create_case(customer_id=None, submitted_values=_submitted_values())
        second_case = service.create_case(customer_id=None, submitted_values=_submitted_values())
        service.link_email(case_id=first_case.id, source_email_key=f"unassigned-{mailbox_email.id}")

        linked_case = service.linked_case_for_source_email(source_email_key=f"unassigned-{mailbox_email.id}")

        assert linked_case is not None
        assert linked_case.case.id == first_case.id
        with pytest.raises(HubCaseError, match="bereits mit einem anderen Fall"):
            service.link_email(case_id=second_case.id, source_email_key=f"unassigned-{mailbox_email.id}")

        service.delete_case(case_id=first_case.id)

        assert service.linked_case_for_source_email(source_email_key=f"unassigned-{mailbox_email.id}") is None


def test_hub_case_rejects_linking_a_customer_email_to_another_customer_case():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        email_customer = Customer(name="E-Mail Kunde", is_visible=True)
        other_customer = Customer(name="Anderer Kunde", is_visible=True)
        customer_email = CustomerZohoEmail(
            customer=email_customer,
            source="zoho",
            direction="inbound",
            is_unread=True,
            encrypted_payload_json=cipher.encrypt('{"subject":"Anfrage"}'),
        )
        db.add_all([email_customer, other_customer, customer_email])
        db.flush()
        service = _service(db)
        case = service.create_case(customer_id=other_customer.id, submitted_values=_submitted_values())

        with pytest.raises(HubCaseError, match="desselben Kunden"):
            service.link_email(
                case_id=case.id,
                source_email_key=f"linked-{email_customer.id}-{customer_email.id}",
            )
