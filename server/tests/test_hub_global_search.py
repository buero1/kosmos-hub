import json
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_activity import CustomerCallActivity
from app.models.customer_contact import CustomerContact
from app.models.hub_case import HubCase
from app.models.hub_lead import HubLead
from app.models.site import Site
from app.services.hub_global_search import HubGlobalSearchService


def test_global_search_groups_records_without_finance_or_email_results():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("test-secret")

    with Session(engine) as db:
        customer = Customer(name="Alpha Baeckerei", website_domain="alpha.example", is_visible=True)
        db.add(customer)
        db.flush()
        db.add_all([
            Site(uuid="11111111-1111-1111-1111-111111111111", customer_id=customer.id,
                 domain="alpha.example", home_url="https://alpha.example", site_url="https://alpha.example"),
            CustomerContact(customer_id=customer.id, encrypted_profile_json=cipher.encrypt(json.dumps({
                "fields": {"Name": "Anna Alpha", "E-Mail": "anna@example.test"}
            }))),
            HubLead(encrypted_profile_json=cipher.encrypt(json.dumps({
                "fields": {"first_name": "Alphons", "last_name": "Beispiel", "company": "Alpha Agentur"}
            }))),
            HubCase(case_number="ALPHA-001", customer_id=customer.id,
                    encrypted_fields_json=cipher.encrypt(json.dumps({"description": "Alpha Anfrage"}))),
            CustomerCallActivity(customer_id=customer.id, name="Alpha Anruf", status="planned",
                                 direction="outbound", starts_at=datetime(2026, 9, 14, 10),
                                 ends_at=datetime(2026, 9, 14, 10, 30), duration_minutes=30),
        ])
        db.flush()

        service = HubGlobalSearchService(db=db, cipher=cipher)
        groups = service.search("alpha", include_admin_modules=True)
        assert [group["key"] for group in groups] == [
            "customers", "contacts", "leads", "cases", "sites", "calendar"
        ]
        assert groups[0]["items"][0]["url"] == f"/customers/{customer.id}"
        assert groups[-1]["items"][0]["url"] == "/calendar?week=2026-09-14"
        assert all(group["key"] not in {"finance", "emails"} for group in groups)

        limited = service.search("alpha", include_admin_modules=False)
        assert [group["key"] for group in limited] == ["customers", "sites", "calendar"]
        assert service.search("a", include_admin_modules=True) == []
        assert service.search("%_", include_admin_modules=True) == []


def test_global_search_limits_results_per_module_and_skips_invalid_encrypted_records():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("test-secret")

    with Session(engine) as db:
        db.add_all(Customer(name=f"Sample {index}", is_visible=True) for index in range(9))
        db.add(CustomerContact(encrypted_profile_json="invalid encrypted value"))
        db.flush()

        groups = HubGlobalSearchService(db=db, cipher=cipher).search("sample", include_admin_modules=True)
        assert [group["key"] for group in groups] == ["customers"]
        assert len(groups[0]["items"]) == 6


def test_global_search_finds_normalized_phone_numbers_in_profiles():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("test-secret")

    with Session(engine) as db:
        customer = Customer(
            name="Nordstern GmbH", is_visible=True,
            encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"Telefon": "+49 89 123 456"}})),
        )
        db.add(customer)
        db.flush()
        contact = CustomerContact(
            customer_id=customer.id,
            encrypted_profile_json=cipher.encrypt(json.dumps({
                "fields": {"Name": "Mara Nord", "Telefon alternativ": "0049 30 987 654"}
            })),
        )
        lead = HubLead(encrypted_profile_json=cipher.encrypt(json.dumps({
            "fields": {"first_name": "Nora", "last_name": "Nord", "mobile": "+49 151 765 432"}
        })))
        db.add_all([contact, lead])
        db.flush()

        service = HubGlobalSearchService(db=db, cipher=cipher)
        customer_groups = service.search("089/123 45", include_admin_modules=True)
        assert [group["key"] for group in customer_groups] == ["customers"]
        assert customer_groups[0]["items"] == [{
            "label": "Nordstern GmbH", "detail": "Telefon: +49 89 123 456", "url": f"/customers/{customer.id}",
        }]

        contact_groups = service.search("030987", include_admin_modules=True)
        assert [group["key"] for group in contact_groups] == ["customers", "contacts"]
        assert contact_groups[0]["items"][0]["url"] == f"/customers/{customer.id}"
        assert contact_groups[1]["items"][0]["url"] == f"/contacts/{contact.id}"
        assert contact_groups[1]["items"][0]["detail"] == "Telefon: 0049 30 987 654"

        lead_groups = service.search("0151765", include_admin_modules=True)
        assert [group["key"] for group in lead_groups] == ["leads"]
        assert lead_groups[0]["items"][0]["url"] == f"/leads/{lead.id}"
        assert [group["key"] for group in service.search("030987", include_admin_modules=False)] == ["customers"]
        assert [group["key"] for group in service.search("+49 (0)30 987", include_admin_modules=True)] == [
            "customers", "contacts"
        ]


def test_global_search_respects_visibility_and_sensitive_phone_metadata():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("test-secret")
    with Session(engine) as db:
        db.add_all([
            Customer(name="Visible", is_visible=True, encrypted_profile_json=cipher.encrypt(json.dumps({
                "fields": {"Telefon": "+49 40 111 222", "Kundennummer": "777888", "Titel": "666777"},
                "field_metadata": {"phone": {"label": "Telefon", "sensitive": True}},
            }))),
            Customer(name="Hidden", is_visible=False, encrypted_profile_json=cipher.encrypt(json.dumps({
                "fields": {"Telefon": "+49 40 333 444"}
            }))),
        ])
        service = HubGlobalSearchService(db=db, cipher=cipher)
        assert service.search("040111", include_admin_modules=False) == []
        assert [group["key"] for group in service.search("040111", include_admin_modules=True)] == ["customers"]
        assert service.search("040333", include_admin_modules=True) == []
        assert service.search("777888", include_admin_modules=True) == []
        assert service.search("666777", include_admin_modules=True) == []
