import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.zoho_email_template import ZohoEmailTemplate
from test_customer_communications import FakeZohoCommunications, _authorize_operator, _customer, _service


@pytest.fixture
def shared_contacts():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, first = _customer(cipher)
        db.add_all([customer, first])
        db.flush()
        second = CustomerContact(customer=customer, encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {
            "Name": "Selected Contact", "E-Mail": "anna@example.de",
            "Zweite E-Mail-Adresse": "anna.private@example.de", "Dritte E-Mail-Adresse": "anna@example.de",
        }})))
        other = Customer(name="Other customer")
        db.add_all([second, other])
        db.add(ZohoEmailTemplate(zoho_template_id="shared-contact", module="Contacts", is_active=True,
            zoho_synced_at=datetime.now(UTC), encrypted_payload_json=cipher.encrypt(json.dumps({
                "name": "Contact test", "subject": "Hello ${Contact.Name}", "content": "<p>${Contact.Name}</p>",
            }))))
        db.commit()
        fake = FakeZohoCommunications()
        yield SimpleNamespace(db=db, customer=customer, first=first, second=second, other=other,
                              cipher=cipher, fake=fake, service=_service(db, fake))
    engine.dispose()


@pytest.mark.parametrize("email", ["anna@example.de", "anna.private@example.de"])
def test_explicit_contact_survives_shared_addresses_and_keeps_template_identity(shared_contacts, email):
    p = shared_contacts
    key = f"contact:{p.second.id}:{email}"
    assert key not in {r.key for r in p.service.list_recipients(customer_id=p.customer.id)}
    recipient = p.service.get_recipient(customer_id=p.customer.id, recipient_key=key)
    assert (recipient.key, recipient.name, recipient.email) == (key, "Selected Contact", email)
    assert len(p.service.list_contact_recipients(customer_id=p.customer.id, allowed_contact_ids={p.second.id})) == 2
    template = p.service.get_email_template(customer_id=p.customer.id, template_id="shared-contact", recipient_key=key)
    assert template.subject == "Hello Selected Contact"
    assert "Selected Contact" in template.content


@pytest.mark.parametrize("invalid", ["foreign", "stale", "missing", "malformed", "oversized"])
def test_invalid_explicit_contact_never_falls_back_to_another_contact(shared_contacts, invalid):
    p = shared_contacts
    customer_id = p.other.id if invalid == "foreign" else p.customer.id
    key = {
        "foreign": f"contact:{p.second.id}:anna@example.de",
        "stale": f"contact:{p.second.id}:old@example.de",
        "missing": "contact:99999:anna@example.de",
        "malformed": "contact:not-an-id:anna@example.de",
        "oversized": f"contact:{'9' * 100}:anna@example.de",
    }[invalid]
    assert p.service.get_recipient(customer_id=customer_id, recipient_key=key) is None
    with pytest.raises(ValueError, match="aktuelle E-Mail-Adresse"):
        p.service.get_email_template(customer_id=customer_id, template_id="shared-contact", recipient_key=key)


@pytest.mark.parametrize("mittwald", [False, True])
def test_both_transports_send_to_the_explicit_shared_contact(shared_contacts, monkeypatch, mittwald):
    p = shared_contacts
    _authorize_operator(p.db, monkeypatch, mittwald=mittwald)
    delivered = []

    def send(_self, **kwargs):
        delivered.append(kwargs)
        return SimpleNamespace(message_id="test@example.de", sent_at=datetime.now(UTC))

    monkeypatch.setattr("app.services.customer_communications.HubMailboxTransportService.send", send)
    if not mittwald:
        with pytest.raises(ValueError, match="Postfach"):
            p.service.send_email(customer_id=p.customer.id, actor="operator", sender_email="team@example.de",
                recipient_key=f"contact:{p.second.id}:anna@example.de", subject="Test", content="<p>Test</p>")
        assert delivered == p.fake.sent_emails == []
        return
    result = p.service.send_email(customer_id=p.customer.id, actor="operator", sender_email="team@example.de",
        recipient_key=f"contact:{p.second.id}:anna@example.de", subject="Test", content="<p>Test</p>")
    assert result.success
    if mittwald:
        assert len(delivered) == 1
        assert delivered[0]["recipient_name"] == "Selected Contact"
        assert delivered[0]["recipient_email"] == "anna@example.de"
    else:
        assert len(p.fake.sent_emails) == 1
        assert p.fake.sent_emails[0][3:5] == ("Selected Contact", "anna@example.de")
