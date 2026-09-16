import json
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_finance_article import HubFinanceArticle
from app.models.hub_finance_documents import HubFinanceOrder
from app.services.zoho_books import ZohoBooksError
from app.services.zoho_books_order_import import ZohoBooksOrderImportService


class FakeBooksOrderReader:
    def __init__(self):
        self.order = {
            "salesorder_id": "7001",
            "salesorder_number": "SO-100",
            "status": "confirmed",
            "date": "2026-07-30",
            "created_time": "2026-07-31T15:39:18+0200",
            "last_modified_time": "2026-08-01T10:00:00+0200",
            "customer_id": "books-customer-1",
            "customer_name": "Beispiel GmbH",
            "reference_number": "EST-100",
            "contact_persons_associated": [{"contact_person_id": "books-contact-1"}],
            "line_items": [
                {
                    "item_id": "books-item-1",
                    "name": "Homepage Basic",
                    "description": "Historischer Vertragspreis",
                    "quantity": 1,
                    "unit": "Monatlich",
                    "rate": "44.99",
                    "discount": "0%",
                    "tax_percentage": 19,
                }
            ],
        }

    @staticmethod
    def get_status():
        return SimpleNamespace(organization_id="books-org-1")

    @staticmethod
    def list_all_sales_orders():
        return ({"salesorder_id": "7001", "customer_id": "books-customer-1"},)

    def get_sales_order(self, *, sales_order_id: str):
        assert sales_order_id == "7001"
        return self.order

    @staticmethod
    def get_books_contact(*, contact_id: str):
        assert contact_id == "books-customer-1"
        return {
            "zcrm_account_id": "2930984000000000001",
            "contact_persons": [
                {
                    "contact_person_id": "books-contact-1",
                    "zcrm_contact_id": "crm-contact-1",
                }
            ],
        }


class FakeCrmOrderReader:
    def __init__(self, records=None):
        self.records = records or [
            {
                "id": "2930984000000000100",
                "Name": "Website-Auftrag",
                "Created_Time": "2026-07-31T15:30:31+02:00",
                "Modified_Time": "2026-08-02T11:00:00+02:00",
                "Aussendienst": "N.Baydar",
                "Vertragsbeginn": "2026-08-01",
                "Vertragsbemerkung": "Aus CRM übernommen",
                "Vertragsdatum": "2026-07-31",
                "Vertragslaufzeit": "12",
                "Zahlungsart": "Lastschrift",
                "Zahlweise": "Monatlich",
                "K_ndigungsfrist": "1 Monat vor Ablauf, Verlängerung um 1 Jahr",
                "Kunden": {"id": "2930984000000000001", "name": "Beispiel GmbH"},
                "Domainwunsch": "beispiel.de",
                "Auftragsaufnahme_Art": "Schriftlich",
                "Vertragsdauer_in_Jahren": 1,
                "K_ndigungsdatum": "2027-06-30",
                "Kontakt": {"id": "crm-contact-1", "name": "Erika Beispiel"},
                "Auftrag_erzielt_von": "N. Baydar",
                "Auftragswertung_Agent": "100%",
            }
        ]

    @staticmethod
    def get_status():
        return SimpleNamespace(connected=True)

    def list_order_records_for_account(self, account_id: str):
        assert account_id == "2930984000000000001"
        return self.records


class FailingBooksOrderReader:
    @staticmethod
    def get_status():
        return SimpleNamespace(organization_id="books-org-1")

    @staticmethod
    def list_all_sales_orders():
        return tuple(
            {"salesorder_id": source_id, "customer_id": f"books-customer-{source_id}"}
            for source_id in ("7001", "7002", "7003", "7004")
        )

    @staticmethod
    def get_sales_order(*, sales_order_id: str):
        raise ZohoBooksError(f"Books failure for {sales_order_id}")

    @staticmethod
    def get_books_contact(*, contact_id: str):
        raise AssertionError("No contact lookup is expected after a failed order request.")


def _seed(db: Session, cipher: SecretCipher):
    customer = Customer(name="Beispiel GmbH", zoho_id="2930984000000000001")
    contact = CustomerContact(
        customer=customer,
        zoho_id="crm-contact-1",
        encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"Name": "Erika Beispiel"}})),
    )
    article = HubFinanceArticle(
        zoho_books_id="books-item-1",
        encrypted_fields_json=cipher.encrypt(json.dumps({"name": "Homepage Basic", "sku": "ART-1"})),
    )
    db.add_all((customer, contact, article))
    db.commit()
    return customer, contact, article


def test_order_import_combines_books_positions_with_reviewed_crm_fields_and_is_idempotent():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer, contact, article = _seed(db, cipher)
        service = ZohoBooksOrderImportService(
            db=db,
            cipher=cipher,
            books_service=FakeBooksOrderReader(),
            crm_service=FakeCrmOrderReader(),
        )

        status, started = service.start_all(requested_by="books-admin")
        db.commit()
        assert started is True
        assert status.total_orders == 1
        assert service.process_next_order() == "succeeded"
        assert service.process_next_order() == "completed"

        order = db.scalar(select(HubFinanceOrder).where(HubFinanceOrder.zoho_books_id == "7001"))
        assert order is not None
        assert order.zoho_crm_id == "2930984000000000100"
        assert order.order_number == "SO-100"
        assert order.customer_id == customer.id
        assert order.contact_id == contact.id
        values = json.loads(cipher.decrypt(order.encrypted_fields_json))
        assert values["order_name"] == "Website-Auftrag"
        assert values["order_date"] == "2026-07-31"
        assert values["payment_method"] == "Lastschrift"
        assert values["domain_request"] == "beispiel.de"
        assert values["agent_order_rating"] == "100%"
        assert len(order.lines) == 1
        assert order.lines[0].article_id == article.id
        line_values = json.loads(cipher.decrypt(order.lines[0].encrypted_fields_json))
        assert line_values["unit_price"] == "44.99"

        final_status = service.status()
        assert final_status is not None
        assert final_status.imported_orders == 1
        assert final_status.crm_matched_orders == 1

        status, started = service.start_all(requested_by="books-admin")
        db.commit()
        assert started is True
        assert service.process_next_order() == "succeeded"
        assert service.process_next_order() == "completed"
        assert db.scalars(select(HubFinanceOrder)).all() == [order]
        assert len(order.lines) == 1


def test_order_import_uses_description_as_name_for_unlinked_free_text_position():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        _seed(db, cipher)
        books = FakeBooksOrderReader()
        books.order["line_items"] = [
            {
                "name": "",
                "description": "Frei erfasste Zusatzleistung",
                "quantity": 1,
                "rate": "25.00",
                "tax_percentage": 19,
            }
        ]
        service = ZohoBooksOrderImportService(
            db=db,
            cipher=cipher,
            books_service=books,
            crm_service=FakeCrmOrderReader(),
        )

        service.start_all(requested_by="books-admin")
        db.commit()
        assert service.process_next_order() == "succeeded"

        order = db.scalar(select(HubFinanceOrder).where(HubFinanceOrder.zoho_books_id == "7001"))
        assert order is not None
        assert len(order.lines) == 1
        assert order.lines[0].article_id is None
        values = json.loads(cipher.decrypt(order.lines[0].encrypted_fields_json))
        assert values["name"] == "Frei erfasste Zusatzleistung"
        assert values["description"] == ""


def test_order_import_normalizes_generic_order_names_to_website():
    assert ZohoBooksOrderImportService._normalized_order_name(
        "Auftrag",
        order_number="SO-100",
    ) == "Website"
    assert ZohoBooksOrderImportService._normalized_order_name(
        "SO-100",
        order_number="SO-100",
    ) == "Website"
    assert ZohoBooksOrderImportService._normalized_order_name(
        "Individueller Projektname",
        order_number="SO-100",
    ) == "Individueller Projektname"


def test_order_import_keeps_ambiguous_crm_orders_unlinked():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("b" * 32)
        _seed(db, cipher)
        crm = FakeCrmOrderReader(records=[
            {"id": "crm-1", "Vertragsdatum": "2026-06-01"},
            {"id": "crm-2", "Vertragsdatum": "2026-08-15"},
        ])
        service = ZohoBooksOrderImportService(
            db=db,
            cipher=cipher,
            books_service=FakeBooksOrderReader(),
            crm_service=crm,
        )

        service.start_all(requested_by="books-admin")
        db.commit()
        assert service.process_next_order() == "succeeded"
        order = db.scalar(select(HubFinanceOrder))
        assert order is not None
        assert order.zoho_crm_id is None
        status = service.status()
        assert status is not None
        assert status.crm_ambiguous_orders == 1
        assert status.failed_orders == 0


def test_order_import_stops_after_three_consecutive_failures():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = ZohoBooksOrderImportService(
            db=db,
            cipher=SecretCipher("c" * 32),
            books_service=FailingBooksOrderReader(),
            crm_service=FakeCrmOrderReader(),
        )
        service.start_all(requested_by="books-admin")
        db.commit()

        assert service.process_next_order() == "failed"
        assert service.process_next_order() == "failed"
        assert service.process_next_order() == "stopped"
        status = service.status()
        assert status is not None
        assert status.status == "stopped"
        assert status.processed_orders == 3
        assert status.failed_orders == 3
        assert status.consecutive_failures == 3
