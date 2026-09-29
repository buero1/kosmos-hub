import json
from datetime import UTC, datetime
from html import escape

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_mailbox_email import HubMailboxEmail
from app.services.customer_communications import CustomerCommunicationService
from app.services.hub_mailbox import HubMailboxService


@pytest.mark.parametrize("url", [
    "https://example[.]org/",
    "https://[unfinished/",
    "https://[not-an-ipv6-address]/",
    "https://[127.0.0.1]/",
    "https://example.org\uff0fother/path",
])
@pytest.mark.parametrize("tag,attribute", [("a", "href"), ("img", "src")])
def test_invalid_urls_do_not_abort_email_sanitization(url, tag, attribute):
    element = f'<{tag} {attribute}="{escape(url, quote=True)}">'
    if tag == "a":
        element += "Website text</a>"
    content = CustomerCommunicationService._sanitized_email_content(
        f'<p>Before {element} after</p><a href="https://example.org/">Safe link</a>'
    )

    assert "Before " in content and " after" in content
    assert url not in content
    assert '<a href="https://example.org/">Safe link</a>' in content
    if tag == "a":
        assert "Website text" in content
    else:
        assert "<img" not in content


@pytest.mark.parametrize("url", [
    "https://example.org/path?a=1&b=2",
    "https://[2001:db8::1]/",
    "mailto:person@example.org",
])
def test_valid_links_including_ipv6_are_preserved(url):
    source = f'<a href="{escape(url, quote=True)}" target="_blank">Website</a>'
    content = CustomerCommunicationService._sanitized_email_content(source)
    assert f'href="{escape(url, quote=True)}"' in content
    assert 'rel="noopener noreferrer"' in content


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///private/file", "data:text/html,unsafe"])
def test_unsafe_link_schemes_remain_blocked(url):
    assert CustomerCommunicationService._sanitized_email_content(
        f'<p><a href="{url}">Visible text</a></p>'
    ) == "<p>Visible text</p>"


@pytest.mark.parametrize("linked", [False, True])
def test_replies_and_forwards_tolerate_defanged_urls_without_changing_original(linked):
    cipher = SecretCipher("a" * 32)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    original = (
        '<p>Please reply.</p><a href="https://example[.]org/">example[.]org</a>'
        '<img src="https://[broken/logo.png"><p>End of message.</p>'
    )
    encrypted = cipher.encrypt(json.dumps({
        "subject": "Question",
        "from": {"name": "Contact", "email": "contact@example.org"},
        "to": [{"email": "team@example.org"}],
        "content": original,
    }))
    with Session(engine) as db:
        if linked:
            customer = Customer(name="Example", encrypted_profile_json=cipher.encrypt(json.dumps({
                "fields": {"Kontakt-E-Mail": "contact@example.org"},
            })))
            db.add(customer)
            db.flush()
            email = CustomerZohoEmail(
                customer_id=customer.id, source="mittwald-imap", direction="inbound",
                zoho_message_id="<original@example.org>", encrypted_payload_json=encrypted,
            )
        else:
            email = HubMailboxEmail(
                source="mittwald-imap", direction="inbound", fingerprint="a" * 64,
                encrypted_payload_json=encrypted,
                received_at=datetime(2026, 9, 29, 10, 0, tzinfo=UTC),
            )
        db.add(email)
        db.flush()
        if linked:
            service = CustomerCommunicationService(db=db, cipher=cipher, public_base_url="https://hub.example")
            reply = service.get_email_reply(customer_id=customer.id, email_id=email.id, allow_fetch=False)
            forward = service.get_email_forward(customer_id=customer.id, email_id=email.id, allow_fetch=False)
            assert reply.recipient_email == "contact@example.org"
            assert reply.subject == "Re: Question"
            assert forward.subject == "Fwd: Question"
            contents = [reply.content, forward.content]
        else:
            service = HubMailboxService(db=db, cipher=cipher, public_base_url="https://hub.example")
            reply = service.get_unassigned_email_compose_context(email_id=email.id, action="reply_all")
            forward = service.get_unassigned_email_compose_context(email_id=email.id, action="forward")
            assert reply["recipient_email"] == "contact@example.org"
            assert reply["subject"] == "Re: Question"
            assert forward["subject"] == "Fwd: Question"
            contents = [reply["content"], forward["content"]]
        for content in contents:
            assert "Please reply." in content and "End of message." in content
            assert "example[.]org</a>" not in content
            assert "example[.]org" in content
            assert "https://example[.]org/" not in content
            assert "https://[broken" not in content
        assert email.encrypted_payload_json == encrypted
        db.rollback()
    engine.dispose()
