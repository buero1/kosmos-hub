import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_user import HubUser
from app.models.site import Site
from app.services.customer_directory import CUSTOMER_FIELDS_LAYOUT_KEY, CustomerDirectoryService
from app.services.module_layouts import ModuleLayoutService


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
        assert [(tab.label, [field.label for field in tab.fields]) for tab in contact_detail.profile_field_tabs] == [
            (
                "Kontakt",
                [
                    "Name",
                    "Anrede",
                    "Position",
                    "E-Mail",
                    "Zweite E-Mail-Adresse",
                    "Dritte E-Mail-Adresse",
                    "Telefon",
                    "Telefon alternativ",
                    "Telefon privat",
                ],
            ),
            ("Postadresse", []),
            ("Weitere Daten", []),
        ]
        editable_contact_keys = {field.key for field in contact_detail.editable_profile_fields}
        assert [field.key for field in contact_detail.editable_profile_fields] == [
            field.key for field in contact_detail.display_profile_fields if field.key in editable_contact_keys
        ]
        assert _service(db).get_contact_detail(customer_id=customer.id + 1, contact_id=first_contact.id) is None


def test_customer_directory_uses_the_picklist_label_for_reading_and_value_for_editing():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer = Customer(
            name="Test Customer",
            zoho_id="zoho-account-test",
            encrypted_profile_json=cipher.encrypt(
                json.dumps(
                    {
                        "fields": {"Kunde Typ": "Analyst"},
                        "field_metadata": {
                            "customer_type": {
                                "label": "Kunde Typ",
                                "display_type": "Auswahlliste",
                                "editable": True,
                                "pick_list_values": [{"value": "Analyst", "label": "Test-Kunde"}],
                            }
                        },
                    }
                )
            ),
        )
        db.add(customer)
        db.commit()

        detail = _service(db).get_detail(customer_id=customer.id)

        assert detail is not None
        customer_type = next(field for field in detail.display_profile_fields if field.key == "customer_type")
        editable_customer_type = next(field for field in detail.editable_profile_fields if field.key == "customer_type")
        assert customer_type.value == "Test-Kunde"
        assert editable_customer_type.form_value == "Analyst"
        assert editable_customer_type.options == (("Analyst", "Test-Kunde"),)


def test_contact_detail_collapses_fields_after_secretary_phone():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer = Customer(name="Example Customer", zoho_id="zoho-account-1")
        contact = CustomerContact(
            customer=customer,
            zoho_id="zoho-contact-1",
            encrypted_profile_json=cipher.encrypt(
                json.dumps(
                    {
                        "fields": {
                            "Name": "Anna Example",
                            "Tel.": "+49 89 123456",
                            "Mobil": "+49 170 1234567",
                            "Telefon alternativ": "+49 89 987654",
                            "Telefon privat": "+49 89 456789",
                            "Telefon Sekr.": "+49 89 111222",
                            "Vorname": "Anna",
                            "Nachname": "Example",
                            "Postadresse Straße": "Musterstraße 1",
                        }
                    }
                )
            ),
        )
        db.add_all([customer, contact])
        db.commit()

        detail = _service(db).get_contact_detail(customer_id=customer.id, contact_id=contact.id)

        assert detail is not None
        assert detail.summary_profile_fields[-1].label == "Telefon Sekr."
        assert detail.following_profile_fields[0].label == "Vorname"
        assert "Postadresse Straße" in [field.label for field in detail.following_profile_fields]


def test_customer_directory_prioritizes_core_fields_and_combines_postal_city_for_the_hub():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer = Customer(
            name="Example Customer",
            encrypted_profile_json=cipher.encrypt(
                json.dumps(
                    {
                        "fields": {
                            "Kunde-Name": "Example Customer",
                            "Status": "Aktuell",
                            "Kunde Typ": "Kunde",
                            "Rechnungsadresse - Straße": "Musterstraße 1",
                            "Rechnungsadresse - PLZ": "82319",
                            "Rechnungsadresse - Stadt": "Starnberg",
                            "Tel.": "+498151123456",
                            "Webseite": "https://example.test",
                            "Arbeitsdomain-Login": "https://example.test/wp-admin",
                            "Options an WP senden": False,
                            "Branche": "Beratung",
                        },
                        "field_metadata": {
                            "customer_name": {"label": "Kunde-Name", "editable": True},
                            "account_status": {"label": "Status"},
                            "customer_type": {"label": "Kunde Typ"},
                            "billing_street": {"label": "Rechnungsadresse - Straße"},
                            "billing_postal_code": {"label": "Rechnungsadresse - PLZ"},
                            "billing_city": {"label": "Rechnungsadresse - Stadt"},
                            "phone": {"label": "Tel."},
                            "website": {"label": "Webseite", "editable": True},
                            "work_domain_login": {"label": "Arbeitsdomain-Login"},
                            "send_options_to_wordpress": {"label": "Options an WP senden"},
                            "industry": {"label": "Branche", "editable": True},
                        },
                    }
                )
            ),
        )
        db.add(customer)
        db.commit()

        detail = _service(db).get_detail(customer_id=customer.id)

        assert detail is not None
        assert [field.label for field in detail.display_profile_fields] == [
            "Kunde-Name",
            "Tel.",
            "Status",
            "Kunde Typ",
            "Webseite",
            "Arbeitsdomain-Login",
            "Rechnungsadresse - Straße",
            "PLZ Ort",
            "Options an WP senden",
            "Branche",
        ]
        assert detail.display_profile_fields[-3].value == "82319 Starnberg"
        assert detail.display_profile_fields[-3].display_type == "Hub-Feld"
        assert [field.label for field in detail.summary_profile_fields] == [
            "Kunde-Name",
            "Tel.",
            "Status",
            "Kunde Typ",
            "Webseite",
            "Arbeitsdomain-Login",
            "Rechnungsadresse - Straße",
            "PLZ Ort",
            "Options an WP senden",
        ]
        assert [field.label for field in detail.following_profile_fields] == ["Branche"]
        assert [field.key for field in detail.editable_profile_fields] == ["customer_name", "website", "industry"]

        admin = HubUser(username="operator", password_hash="hashed", role="admin")
        db.add(admin)
        db.flush()
        default_keys = tuple(field.key for field in detail.display_profile_fields)
        ModuleLayoutService(db=db).configure(
            actor=admin,
            layout_key=CUSTOMER_FIELDS_LAYOUT_KEY,
            item_order_json=json.dumps(("industry",) + default_keys[:-1]),
            allowed_keys=default_keys,
        )
        db.commit()

        reordered = _service(db).get_detail(customer_id=customer.id)

        assert reordered is not None
        assert reordered.display_profile_fields[0].label == "Branche"
        assert [field.key for field in reordered.editable_profile_fields] == ["industry", "customer_name", "website"]


def test_customer_directory_groups_existing_fields_into_synchronized_tabs():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer = Customer(
            name="Example Customer",
            encrypted_profile_json=cipher.encrypt(
                json.dumps(
                    {
                        "fields": {
                            "Bank": "Musterbank",
                            "Bankverbindung zeigen": True,
                            "BIC": "MUSTERBIC",
                            "IBAN": "DE02120300000000202051",
                            "Kontoinhaber": "Erika Muster",
                            "Art SEPA-Erteilung": "Online",
                            "Datum SEPA-Erteilung": "2026-01-15",
                            "Dialfire Kampagnename": "Gewinnung 2026",
                            "Dialfire-API-Token": "secret-token",
                            "Dialfire-ID": "dialfire-42",
                            "Dauer in Minuten": "30",
                            "Google Kalender": "Kalender A",
                            "Termin-Erinnerung setzen?": True,
                            "Terminzeit": "2026-02-20T10:00:00+01:00",
                        },
                        "field_metadata": {
                            "bank": {"label": "Bank"},
                            "show_bank_details": {"label": "Bankverbindung zeigen"},
                            "bic": {"label": "BIC"},
                            "iban": {"label": "IBAN", "sensitive": True},
                            "account_holder": {"label": "Kontoinhaber"},
                            "sepa_grant_type": {"label": "Art SEPA-Erteilung"},
                            "sepa_grant_date": {"label": "Datum SEPA-Erteilung"},
                            "dialfire_campaign_name": {"label": "Dialfire Kampagnename"},
                            "dialfire_api_token": {"label": "Dialfire-API-Token", "sensitive": True},
                            "dialfire_id": {"label": "Dialfire-ID"},
                            "duration_minutes": {"label": "Dauer in Minuten"},
                            "google_calendar": {"label": "Google Kalender"},
                            "appointment_reminder": {"label": "Termin-Erinnerung setzen?"},
                            "appointment_at": {"label": "Terminzeit"},
                        },
                    }
                )
            ),
        )
        db.add(customer)
        db.commit()

        detail = _service(db).get_detail(customer_id=customer.id, include_sensitive=True)

        assert detail is not None
        tabs = {tab.key: tab for tab in detail.profile_field_tabs}
        assert [tab.label for tab in detail.profile_field_tabs] == ["Bankverbindung", "Dialfire", "Termin"]
        assert [field.label for field in tabs["bank-details"].fields] == [
            "Bank",
            "Bankverbindung zeigen",
            "BIC",
            "IBAN",
            "Kontoinhaber",
            "Art SEPA-Erteilung",
            "Datum SEPA-Erteilung",
        ]
        assert [field.label for field in tabs["dialfire"].fields] == [
            "Dialfire Kampagnename",
            "Dialfire-API-Token",
            "Dialfire-ID",
        ]
        assert [field.label for field in tabs["appointments"].fields] == [
            "Dauer in Minuten",
            "Google Kalender",
            "Termin-Erinnerung setzen?",
            "Terminzeit",
        ]
        assert tabs["bank-details"].fields[3].value == "Geschützt"
        assert tabs["dialfire"].fields[1].value == "Geschützt"
        assert all(field in detail.display_profile_fields for tab in tabs.values() for field in tab.fields)


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


def test_customer_directory_masks_sensitive_zoho_profile_values_for_admins_and_hides_them_for_others():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer = Customer(
            name="Protected Customer",
            encrypted_profile_json=cipher.encrypt(
                json.dumps(
                    {
                        "fields": {"IBAN": "DE02120300000000202051", "Webseite": "https://example.test"},
                        "field_metadata": {
                            "iban": {"label": "IBAN", "sensitive": True, "editable": True},
                            "website": {"label": "Webseite", "editable": True},
                        },
                    }
                )
            ),
        )
        db.add(customer)
        db.commit()

        public_detail = _service(db).get_detail(customer_id=customer.id)
        admin_detail = _service(db).get_detail(customer_id=customer.id, include_sensitive=True)

        assert public_detail is not None
        assert [field.label for field in public_detail.profile_fields] == ["Webseite"]
        assert admin_detail is not None
        iban = next(field for field in admin_detail.profile_fields if field.label == "IBAN")
        assert iban.value == "Geschützt"
        assert iban.form_value == ""


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


def test_hub_contacts_can_be_created_without_zoho_or_customer_and_linked_later():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = _service(db)
        contact = service.create_hub_contact(
            customer_id=None,
            submitted_values={
                "contact_field__salutation": "Frau",
                "contact_field__first_name": "Anna",
                "contact_field__last_name": "Hub",
            },
        )
        db.commit()

        assert contact.customer_id is None
        assert contact.zoho_id is None
        profile = json.loads(SecretCipher("a" * 32).decrypt(contact.encrypted_profile_json))
        assert profile["source"] == "hub"
        assert profile["fields"]["Name"] == "Anna Hub"

        entry = service.list_contact_entries()[0]
        assert entry.customer is None
        assert entry.contact.is_hub_contact is True

        customer = Customer(name="Linked Later", zoho_id="zoho-linked-later")
        db.add(customer)
        db.flush()
        service.set_hub_contact_customer(contact_id=contact.id, customer_id=customer.id)
        service.set_hub_contact_customer(contact_id=contact.id, customer_id=None)
        db.commit()

        assert db.get(CustomerContact, contact.id) is not None
        service.delete_contact_from_hub(contact_id=contact.id)
        db.commit()
        assert db.get(CustomerContact, contact.id) is None
