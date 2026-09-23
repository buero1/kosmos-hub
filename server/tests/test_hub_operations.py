import json
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from mailbox_fixture_helpers import mailbox_account

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_agent import HubAgentAction, HubAgentJob
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.models.hub_finance_offer import HubFinanceOffer
from app.models.hub_lead import HubLead
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.hub_user import HubUser
from app.services.email_attachment_storage import EmailAttachmentStorage
from app.services.hub_agent import HubAgentError, HubAgentService
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_finance import HubFinanceService
from app.services.hub_finance_field_catalog import HubFinanceField
from app.services.hub_finance_pdf_generation import HubFinancePdfError, HubFinancePdfService
from app.services.hub_operations import HubOperationError, HubOperationService, agent_operations, get_operation


def _offer_values() -> dict[str, str]:
    return {
        "lead_name": "Lena Leitner",
        "offer_field__status": "draft",
        "offer_field__offer_date": "2026-09-19",
        "offer_field__valid_until": "2026-10-19",
        "offer_field__currency": "EUR",
        "offer_field__reference": "Neue Website",
        "offer_line__0__name": "Website-Paket",
        "offer_line__0__quantity": "1",
        "offer_line__0__unit": "Einmalig",
        "offer_line__0__unit_price": "100",
        "offer_line__0__discount_percent": "0",
        "offer_line__0__tax_rate": "19",
    }


def _lead(db: Session, cipher: SecretCipher) -> HubLead:
    lead = HubLead(encrypted_profile_json=cipher.encrypt(json.dumps({
        "schema_version": 1, "source": "hub",
        "fields": {"first_name": "Lena", "last_name": "Leitner", "company": "Leitner Design", "email": "lena@example.test"},
        "subforms": {},
    })))
    db.add(lead)
    db.flush()
    return lead


def _agent_action(db: Session, cipher: SecretCipher, values: dict[str, str]) -> HubAgentAction:
    job = HubAgentJob(
        created_by_username="seller", status="ready",
        encrypted_request_json=cipher.encrypt(json.dumps({"instruction": "Angebot erstellen"})),
        encrypted_plan_json=cipher.encrypt(json.dumps({"summary": "Angebot", "response": "Angebot"})),
    )
    db.add(job)
    db.flush()
    action = HubAgentAction(
        job_id=job.id, sort_order=0, action_type="finance.offers.create", status="proposed",
        encrypted_payload_json=cipher.encrypt(json.dumps({
            "action_type": "finance.offers.create", "title": "Angebot erstellen",
            "details": "Neue Website", "input": values,
        })),
    )
    db.add(action)
    db.flush()
    return action


def test_human_and_agent_use_the_same_registered_offer_operation():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        db.add(HubUser(username="seller", password_hash="x", role="admin"))
        lead = _lead(db, cipher)
        human_result = HubOperationService(db=db, cipher=cipher, actor="seller").execute(
            "finance.offers.create", {**_offer_values(), "lead_name": "", "lead_id": str(lead.id)},
        )
        action = _agent_action(db, cipher, _offer_values())
        agent_result = HubAgentService(db=db, cipher=cipher).execute_action(action_id=action.id, actor="seller")
        db.commit()

        offers = db.scalars(select(HubFinanceOffer).order_by(HubFinanceOffer.id)).all()
        assert [offer.lead_id for offer in offers] == [lead.id, lead.id]
        assert human_result.record_id == offers[0].id
        assert agent_result.status == "completed"
        assert agent_result.result_href == f"/finance/offers/{offers[1].id}"
        assert agent_result.background_token
        assert len(db.scalars(select(HubFinanceGeneratedPdf)).all()) == 2
        assert "finance.offers.create" in {operation.key for operation in agent_operations()}


def test_offer_operation_inherits_form_defaults_for_every_position():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        db.add(HubUser(username="seller", password_hash="x", role="admin"))
        lead = _lead(db, cipher)
        values = {
            "lead_id": str(lead.id),
            "offer_field__offer_date": "2026-01-31",
            "offer_line__0__name": "Website",
            "offer_line__0__unit": "Einmalig",
            "offer_line__0__unit_price": "100",
            "offer_line__1__name": "Betreuung",
            "offer_line__1__unit_price": "25",
            "offer_line__1__tax_rate": "7",
        }
        operation = get_operation("finance.offers.create")
        assert operation is not None
        resolved = operation.apply_defaults(values)
        assert resolved["offer_field__valid_until"] == "2026-02-28"
        assert resolved["offer_line__1__quantity"] == "1"
        assert resolved["offer_line__1__tax_rate"] == "7"
        assert "offer_field__payment_terms" not in resolved
        assert "offer_field__reference" not in resolved
        assert "offer_field__status=draft" in operation.agent_description()
        assert "Gültig bis: 2026-02-28" in operation.preview(resolved)
        assert any("Betreuung" in line and "7 % MwSt." in line for line in operation.preview(resolved))

        result = HubOperationService(db=db, cipher=cipher, actor="seller").execute("finance.offers.create", values)
        offer = db.get(HubFinanceOffer, result.record_id)
        assert offer is not None
        fields = json.loads(cipher.decrypt(offer.encrypted_fields_json))
        assert fields["status"] == "draft"
        assert fields["currency"] == "EUR"
        assert fields["valid_until"] == "2026-02-28"
        assert fields["payment_terms"] == ""
        assert fields["reference"] == ""
        assert len(offer.lines) == 2
        second_line = json.loads(cipher.decrypt(offer.lines[1].encrypted_fields_json))
        assert second_line["quantity"] == "1.00"
        assert second_line["unit"] == ""
        assert second_line["tax_rate"] == "7"
        assert second_line["discount_percent"] == "0.00"


def test_operation_contract_tracks_module_fields_and_current_defaults(monkeypatch):
    from app.services import hub_operation_offers

    operation = get_operation("finance.offers.create")
    assert operation is not None
    contract = operation.input_contract({"offer_field__offer_date": "2026-01-31"})
    assert contract["offer_field__payment_terms"]["required"] is False
    assert "default" not in contract["offer_field__payment_terms"]
    assert contract["offer_field__payment_terms"]["options"]["14_days"] == "14 Tage"
    assert contract["offer_field__status"]["required"] is True
    assert contract["offer_field__status"]["default"] == "draft"
    assert contract["offer_field__valid_until"]["default"] == "2026-02-28"
    assert "offer_field__offer_number" not in contract

    # Changing a module field must update agent discovery without another rule.
    fields = tuple(
        replace(field, required=True) if field.key == "reference" else field
        for field in hub_operation_offers.OFFER_FIELDS
    ) + (HubFinanceField("internal_note", "Interne Notiz", "Text"),)
    monkeypatch.setattr(hub_operation_offers, "OFFER_FIELDS", fields)
    updated = operation.input_contract()
    assert updated["offer_field__reference"]["required"] is True
    assert "default" not in updated["offer_field__reference"]
    assert updated["offer_field__internal_note"] == {"label": "Interne Notiz", "required": False}
    assert json.dumps(updated["offer_field__internal_note"], ensure_ascii=False) in operation.agent_description()


def test_offer_operation_denies_missing_module_permission():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        db.add(HubUser(username="viewer", password_hash="x", role="viewer"))
        _lead(db, cipher)
        with pytest.raises(HubOperationError, match="nicht angelegt"):
            HubOperationService(db=db, cipher=cipher, actor="viewer").execute("finance.offers.create", _offer_values())
        assert db.scalars(select(HubFinanceOffer)).all() == []


def test_offer_operation_links_customer_and_its_contact():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        db.add(HubUser(username="seller", password_hash="x", role="admin"))
        customer = Customer(name="Website Kunde")
        db.add(customer)
        db.flush()
        contact = CustomerContact(customer=customer, encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"Name": "Test Kontakt"}})))
        db.add(contact)
        db.flush()
        values = {**_offer_values(), "lead_name": "", "customer_name": customer.name, "contact_id": str(contact.id)}

        result = HubOperationService(db=db, cipher=cipher, actor="seller").execute("finance.offers.create", values)

        offer = db.get(HubFinanceOffer, result.record_id)
        assert offer is not None
        assert offer.customer_id == customer.id
        assert offer.contact_id == contact.id
        assert offer.lead_id is None


def test_offer_operation_hides_unassigned_leads():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        db.add(HubUser(username="sales", password_hash="x", role="sales"))
        HubAccessControlService(db=db).ensure_defaults()
        _lead(db, cipher)

        with pytest.raises(HubOperationError, match="nicht eindeutig"):
            HubOperationService(db=db, cipher=cipher, actor="sales").execute("finance.offers.create", _offer_values())
        assert db.scalars(select(HubFinanceOffer)).all() == []


def test_failed_agent_offer_rolls_back_partial_offer(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        db.add(HubUser(username="seller", password_hash="x", role="admin"))
        _lead(db, cipher)
        action = _agent_action(db, cipher, _offer_values())
        monkeypatch.setattr(HubFinancePdfService, "queue", lambda *_args, **_kwargs: (_ for _ in ()).throw(HubFinancePdfError("PDF fehlgeschlagen")))

        result = HubAgentService(db=db, cipher=cipher).execute_action(action_id=action.id, actor="seller")
        db.commit()

        assert result.status == "failed"
        assert result.error == "PDF fehlgeschlagen"
        assert db.scalars(select(HubFinanceOffer)).all() == []


def test_agent_offer_pdf_becomes_persistent_email_draft_attachment(tmp_path, monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    original_storage_init = EmailAttachmentStorage.__init__

    def local_storage(self, *, root, cipher, min_free_bytes):
        original_storage_init(self, root=tmp_path / "attachments", cipher=cipher, min_free_bytes=0)

    monkeypatch.setattr(EmailAttachmentStorage, "__init__", local_storage)
    with Session(engine) as db:
        db.add(HubUser(username="seller", password_hash="x", role="admin"))
        _lead(db, cipher)
        offer_action = _agent_action(db, cipher, _offer_values())
        mailbox_account(db, cipher)
        draft_action = HubAgentAction(
            job_id=offer_action.job_id, sort_order=1, action_type="emails.drafts.create", status="proposed",
            encrypted_payload_json=cipher.encrypt(json.dumps({
                "action_type": "emails.drafts.create", "title": "E-Mail vorbereiten",
                "details": "Angebot als PDF anhängen", "input": {
                    "recipient_email": "{{action.1.recipient_email}}",
                    "recipient_name": "{{action.1.recipient_name}}",
                    "lead_id": "{{action.1.lead_id}}",
                    "subject": "Angebot für Ihre Website",
                    "content": "<p>Hier ist unser Angebot.</p>",
                    "attachment_ref": "{{action.1.artifact_ref}}",
                },
            })),
        )
        db.add(draft_action)
        db.flush()
        agent = HubAgentService(db=db, cipher=cipher)

        waiting_for_offer = agent.execute_action(action_id=draft_action.id, actor="seller")
        assert waiting_for_offer.status == "proposed"
        assert "vorherige Aktion" in waiting_for_offer.error
        offer_result = agent.execute_action(action_id=offer_action.id, actor="seller")
        assert offer_result.status == "completed"
        waiting_for_pdf = agent.execute_action(action_id=draft_action.id, actor="seller")
        assert waiting_for_pdf.status == "proposed"
        assert "PDF wird noch erzeugt" in waiting_for_pdf.error
        generated = db.scalar(select(HubFinanceGeneratedPdf))
        generated.status = "ready"
        generated.filename = "ANG-000001.pdf"
        generated.storage_key = "test-pdf"
        monkeypatch.setattr(HubFinancePdfService, "load", lambda *_args, **_kwargs: (generated, b"%PDF-test-offer"))

        drafted = agent.execute_action(action_id=draft_action.id, actor="seller")
        assert drafted.status == "completed"
        draft = db.scalar(select(HubMailboxEmail).where(HubMailboxEmail.source == "hub-draft"))
        assert draft is not None
        from app.services.hub_mailbox import HubMailboxService
        mailbox = HubMailboxService(db=db, cipher=cipher, public_base_url="https://hub.example.test")
        context = mailbox.get_draft_compose_context(draft_id=draft.id)
        assert context["recipient_email"] == "lena@example.test"
        assert context["attachments"][0]["filename"] == "ANG-000001.pdf"
        assert mailbox.draft_attachments(draft_id=draft.id)[0].content == b"%PDF-test-offer"
        mailbox.save_draft(
            draft_id=draft.id, sender_email="info@example.test", recipient_email="lena@example.test",
            recipient_key="", recipient_customer_id=None, recipient_name="Lena",
            subject="Korrigierter Betreff", content="<p>Neu</p>", cc_emails="", template_id="",
            reply_to_email_id="", forward_from_email_id="",
        )
        assert mailbox.draft_attachments(draft_id=draft.id)[0].content == b"%PDF-test-offer"
        wrong_recipient = HubAgentAction(
            job_id=offer_action.job_id, sort_order=2, action_type="emails.drafts.create", status="proposed",
            encrypted_payload_json=cipher.encrypt(json.dumps({
                "action_type": "emails.drafts.create", "title": "Falscher Empfänger",
                "details": "Darf nicht angelegt werden", "input": {
                    "recipient_email": "other@example.test", "subject": "Angebot",
                    "content": "<p>Hallo</p>", "attachment_ref": "{{action.1.artifact_ref}}",
                },
            })),
        )
        db.add(wrong_recipient)
        db.flush()
        denied = agent.execute_action(action_id=wrong_recipient.id, actor="seller")
        assert denied.status == "failed"
        assert "Angebotskontakt" in denied.error
        assert len(db.scalars(select(HubMailboxEmail).where(HubMailboxEmail.source == "hub-draft")).all()) == 1


def test_agent_action_references_cannot_cross_jobs():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        db.add(HubUser(username="seller", password_hash="x", role="admin"))
        _lead(db, cipher)
        first = _agent_action(db, cipher, _offer_values())
        second = _agent_action(db, cipher, {**_offer_values(), "lead_id": "{{action.1.record_id}}"})
        failed = HubAgentService(db=db, cipher=cipher).execute_action(action_id=second.id, actor="seller")
        assert failed.status == "failed"
        assert "selben Plan" in failed.error
        assert db.scalars(select(HubFinanceOffer)).all() == []


def test_human_draft_save_uses_gateway_and_keeps_partial_address():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        mailbox_account(db, cipher)
        db.add(HubUser(username="seller", password_hash="x", role="admin"))
        service = HubOperationService(db=db, cipher=cipher, actor="seller")
        partial = service.execute("emails.drafts.save", {"recipient_email": "lena@", "subject": "Angebot"})
        updated = service.execute("emails.drafts.save", {
            "draft_id": str(partial.record_id), "recipient_email": "lena@example.test", "subject": "Angebot",
            "content": "<p>Hallo</p>",
        })
        assert updated.record_id == partial.record_id
        assert db.scalar(select(HubMailboxEmail).where(HubMailboxEmail.id == updated.record_id)) is not None
        assert "emails.drafts.save" not in {operation.key for operation in agent_operations()}


def test_agent_plan_rejects_forward_action_references():
    with pytest.raises(HubAgentError, match="früherer Aktionen"):
        HubAgentService._normalize_plan({
            "summary": "Angebot", "response": "Vorbereitet", "actions": [{
                "action_type": "emails.drafts.create", "title": "Entwurf", "details": "PDF",
                "input": {"attachment_ref": "{{action.2.artifact_ref}}"},
            }],
        })
