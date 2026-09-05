import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.customer_communication import CustomerZohoEmail
from app.models.site import Site
from app.services.customer_directory import CustomerDirectoryService


def _site(*, site_id: int, domain: str, customer_id: int | None = None) -> Site:
    return Site(
        id=site_id,
        uuid=f"a1b2c3d4-0000-4000-8000-{site_id:012d}",
        domain=domain,
        home_url=f"https://{domain}/",
        site_url=f"https://{domain}/",
        customer_id=customer_id,
    )


def _service(db: Session) -> CustomerDirectoryService:
    return CustomerDirectoryService(db=db, cipher=SecretCipher("a" * 32))


def test_customer_directory_requires_explicit_review_before_linking_exact_domain():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Example Customer", zoho_id="zoho-1", website_domain="example-customer.de")
        site = _site(site_id=1, domain="www.example-customer.de")
        db.add_all([customer, site])
        db.commit()

        service = _service(db)
        entry = service.list_entries()[0]
        assert entry.customer.id == customer.id
        assert entry.exact_match_candidate is not None
        assert entry.exact_match_candidate.id == site.id
        assert db.get(Site, site.id).customer_id is None

        service.link_exact_match(customer_id=customer.id, site_id=site.id)
        assert db.get(Site, site.id).customer_id == customer.id


def test_customer_directory_rejects_non_matching_or_ambiguous_sites():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Example Customer", zoho_id="zoho-1", website_domain="example-customer.de")
        same_domain_first = _site(site_id=1, domain="example-customer.de")
        same_domain_second = _site(site_id=2, domain="www.example-customer.de")
        other_site = _site(site_id=3, domain="other-customer.de")
        db.add_all([customer, same_domain_first, same_domain_second, other_site])
        db.commit()

        service = _service(db)
        assert service.list_entries()[0].exact_match_candidate is None

        try:
            service.link_exact_match(customer_id=customer.id, site_id=other_site.id)
        except ValueError as exc:
            assert "exact Zoho website-domain match" in str(exc)
        else:
            raise AssertionError("A non-matching site must not be linkable.")

        try:
            service.link_exact_match(customer_id=customer.id, site_id=same_domain_first.id)
        except ValueError as exc:
            assert "ambiguous" in str(exc)
        else:
            raise AssertionError("An ambiguous domain must not be linkable.")


def test_customer_directory_exposes_status_and_decrypted_profile_fields():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer = Customer(
            name="Example Customer",
            zoho_id="zoho-1",
            encrypted_profile_json=cipher.encrypt(
                json.dumps(
                    {
                        "fields": {
                            "Status": "Aktuell",
                            "Kontakt-E-Mail": "team@example-customer.de",
                            "Rechnungsadresse - Stadt": "Muenchen",
                        }
                    }
                )
            ),
        )
        first_contact = CustomerContact(
            customer=customer,
            zoho_id="zoho-contact-1",
            encrypted_profile_json=cipher.encrypt(
                json.dumps(
                    {
                        "fields": {
                            "Name": "Anna Example",
                            "Anrede": "Frau",
                            "Position": "Managing Director",
                            "E-Mail": "anna@example-customer.de",
                            "Zweite E-Mail-Adresse": "anna.private@example-customer.de",
                            "Dritte E-Mail-Adresse": "anna.other@example-customer.de",
                            "Telefon": "+49 89 123456-1",
                            "Telefon alternativ": "+49 89 123456-2",
                            "Telefon privat": "+49 89 123456-3",
                        }
                    }
                )
            ),
        )
        second_contact = CustomerContact(
            customer=customer,
            zoho_id="zoho-contact-2",
            encrypted_profile_json=cipher.encrypt(
                json.dumps(
                    {
                        "fields": {
                            "Name": "Max Example",
                            "Mobil": "+49 170 1234567",
                        }
                    }
                )
            ),
        )
        db.add_all([customer, first_contact, second_contact])
        db.commit()

        entry = _service(db).list_entries()[0]
        detail = _service(db).get_detail(customer_id=customer.id)

        assert entry.account_status == "Aktuell"
        assert len(_service(db).list_entries(query="muenchen", status="Aktuell")) == 1
        assert _service(db).list_entries(status="Neu") == []
        assert detail is not None
        assert [(field.label, field.value) for field in detail.profile_fields] == [
            ("Status", "Aktuell"),
            ("Kontakt-E-Mail", "team@example-customer.de"),
            ("Rechnungsadresse - Stadt", "Muenchen"),
        ]
        assert [(contact.name, contact.email, contact.mobile) for contact in detail.contacts] == [
            ("Anna Example", "anna@example-customer.de", None),
            ("Max Example", None, "+49 170 1234567"),
        ]
        assert detail.contacts[0].id == first_contact.id
        assert detail.contacts[0].salutation == "Frau"
        assert detail.contacts[0].secondary_email == "anna.private@example-customer.de"
        assert detail.contacts[0].third_email == "anna.other@example-customer.de"
        assert detail.contacts[0].alternate_phone == "+49 89 123456-2"
        assert detail.contacts[0].private_phone == "+49 89 123456-3"

        contact_detail = _service(db).get_contact_detail(customer_id=customer.id, contact_id=first_contact.id)
        assert contact_detail is not None
        assert contact_detail.customer.id == customer.id
        assert [(field.label, field.value) for field in contact_detail.profile_fields] == [
            ("Name", "Anna Example"),
            ("Anrede", "Frau"),
            ("Position", "Managing Director"),
            ("E-Mail", "anna@example-customer.de"),
            ("Zweite E-Mail-Adresse", "anna.private@example-customer.de"),
            ("Dritte E-Mail-Adresse", "anna.other@example-customer.de"),
            ("Telefon", "+49 89 123456-1"),
            ("Telefon alternativ", "+49 89 123456-2"),
            ("Telefon privat", "+49 89 123456-3"),
        ]
        assert _service(db).get_contact_detail(customer_id=customer.id + 1, contact_id=first_contact.id) is None


def test_customer_directory_lists_and_filters_zoho_industries():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        craft_customer = Customer(
            name="Craft Customer",
            encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"Branche": "Handwerk"}})),
        )
        consulting_customer = Customer(
            name="Consulting Customer",
            encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"Branche": "Beratung"}})),
        )
        duplicate_industry_customer = Customer(
            name="Second Craft Customer",
            encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"Branche": "handwerk"}})),
        )
        db.add_all([craft_customer, consulting_customer, duplicate_industry_customer])
        db.commit()

        service = _service(db)
        assert service.list_industries() == ["Beratung", "Handwerk"]
        assert [entry.customer.id for entry in service.list_entries(industry="Handwerk")] == [
            craft_customer.id,
            duplicate_industry_customer.id,
        ]
        assert service.list_entries(industry="Nicht vorhanden") == []


def test_customer_directory_filters_customers_with_unread_inbound_emails():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        unread_customer = Customer(name="Unread Customer")
        read_customer = Customer(name="Read Customer")
        spam_customer = Customer(name="Spam Customer")
        trash_customer = Customer(name="Trash Customer")
        db.add_all(
            [
                unread_customer,
                read_customer,
                spam_customer,
                trash_customer,
                CustomerZohoEmail(
                    customer=unread_customer,
                    zoho_message_id="email-unread",
                    source="zoho",
                    direction="inbound",
                    is_unread=True,
                    encrypted_payload_json="encrypted",
                ),
                CustomerZohoEmail(
                    customer=read_customer,
                    zoho_message_id="email-read",
                    source="zoho",
                    direction="inbound",
                    is_unread=False,
                    encrypted_payload_json="encrypted",
                ),
                CustomerZohoEmail(
                    customer=spam_customer,
                    zoho_message_id="email-spam",
                    source="zoho",
                    direction="inbound",
                    is_unread=True,
                    mailbox_state="spam",
                    encrypted_payload_json="encrypted",
                ),
                CustomerZohoEmail(
                    customer=trash_customer,
                    zoho_message_id="email-trash",
                    source="zoho",
                    direction="inbound",
                    is_unread=True,
                    mailbox_state="trash",
                    encrypted_payload_json="encrypted",
                ),
            ]
        )
        db.commit()

        assert [entry.customer.id for entry in _service(db).list_entries(unread_email_only=True)] == [unread_customer.id]


def test_customer_directory_finds_customer_and_contact_phone_numbers_across_common_formats():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer = Customer(
            name="Phone Search Customer",
            zoho_id="zoho-phone-search",
            encrypted_profile_json=cipher.encrypt(
                json.dumps({"fields": {"Tel.": "0049 (89) / 123-456"}})
            ),
        )
        contact = CustomerContact(
            customer=customer,
            zoho_id="zoho-contact-phone-search",
            encrypted_profile_json=cipher.encrypt(
                json.dumps(
                    {
                        "fields": {
                            "Name": "Phone Contact",
                            "Telefon alternativ": "089-555 / 123",
                            "Telefon privat": "089 777 456",
                            "Mobil": "+49 171-222 333",
                            "Interne Kontakt-Notiz": "Only this contact has the search phrase",
                        }
                    }
                )
            ),
        )
        db.add_all([customer, contact])
        db.commit()

        service = _service(db)
        assert [entry.customer.id for entry in service.list_entries(query="089123456")] == [customer.id]
        assert [entry.customer.id for entry in service.list_entries(query="+49 89 123-456")] == [customer.id]
        assert [entry.customer.id for entry in service.list_entries(query="89/555123")] == [customer.id]
        assert [entry.customer.id for entry in service.list_entries(query="0049 171 222333")] == [customer.id]
        assert [entry.customer.id for entry in service.list_entries(query="only this contact")] == [customer.id]
