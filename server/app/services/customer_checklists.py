from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.base import utcnow
from app.models.customer import Customer
from app.models.customer_checklist import CustomerChecklist, CustomerChecklistItem


DEFAULT_DESIGN_CHECKLIST = (
    "Texterstellung - optional",
    "Mobiloptimierung",
    "Einstellungen WordPress: Seitentitel, Favicon und Startseite",
    "Impressum einrichten",
    "Datenschutzerklärung kontrollieren: Google Fonts, Maps, YouTube usw.",
    "Kontaktformulare testen",
    "E-Mail- und Telefonlinks testen",
    "Korrekturlesung",
    "Browsertests",
)


class CustomerChecklistError(ValueError):
    pass


@dataclass(frozen=True)
class CustomerChecklistItemView:
    id: int
    text: str
    is_completed: bool


@dataclass(frozen=True)
class CustomerChecklistView:
    id: int
    title: str
    items: tuple[CustomerChecklistItemView, ...]
    is_completed: bool


class CustomerChecklistService:
    def __init__(self, *, db: Session):
        self.db = db

    def initialize_pending_customers(self) -> int:
        customers = tuple(
            self.db.scalars(select(Customer).where(Customer.checklists_initialized.is_(False)).order_by(Customer.id))
        )
        for customer in customers:
            self.initialize_customer(customer)
        return len(customers)

    def initialize_customer(self, customer: Customer) -> None:
        if customer.checklists_initialized:
            return
        checklist = CustomerChecklist(customer=customer, title="Design Seiten", sort_order=1)
        checklist.items = [
            CustomerChecklistItem(text=text, sort_order=index)
            for index, text in enumerate(DEFAULT_DESIGN_CHECKLIST, start=1)
        ]
        customer.checklists_initialized = True
        self.db.add(checklist)
        self.db.flush()

    def list_for_customer(self, *, customer_id: int) -> tuple[CustomerChecklistView, ...]:
        self._customer_or_error(customer_id)
        checklists = tuple(
            self.db.scalars(
                select(CustomerChecklist)
                .options(selectinload(CustomerChecklist.items))
                .where(CustomerChecklist.customer_id == customer_id)
                .order_by(CustomerChecklist.sort_order, CustomerChecklist.id)
            ).unique()
        )
        return tuple(self._view(checklist) for checklist in checklists)

    def create_checklist(self, *, customer_id: int, title: str) -> CustomerChecklist:
        self._customer_or_error(customer_id)
        checklist = CustomerChecklist(
            customer_id=customer_id,
            title=self._title(title),
            sort_order=self._next_checklist_order(customer_id),
        )
        self.db.add(checklist)
        self.db.flush()
        return checklist

    def update_checklist(self, *, customer_id: int, checklist_id: int, title: str) -> CustomerChecklist:
        checklist = self._checklist_or_error(customer_id, checklist_id)
        checklist.title = self._title(title)
        self.db.flush()
        return checklist

    def delete_checklist(self, *, customer_id: int, checklist_id: int) -> None:
        checklist = self._checklist_or_error(customer_id, checklist_id)
        self.db.delete(checklist)
        self.db.flush()

    def reorder_checklists(self, *, customer_id: int, ordered_ids: tuple[int, ...]) -> None:
        checklists = tuple(
            self.db.scalars(
                select(CustomerChecklist)
                .where(CustomerChecklist.customer_id == customer_id)
                .order_by(CustomerChecklist.sort_order, CustomerChecklist.id)
            )
        )
        self._validate_order(ordered_ids, tuple(checklist.id for checklist in checklists), "Checklisten")
        by_id = {checklist.id: checklist for checklist in checklists}
        for index, checklist_id in enumerate(ordered_ids, start=1):
            by_id[checklist_id].sort_order = index
        self.db.flush()

    def create_item(self, *, customer_id: int, checklist_id: int, text: str) -> CustomerChecklistItem:
        checklist = self._checklist_or_error(customer_id, checklist_id)
        item = CustomerChecklistItem(
            checklist_id=checklist.id,
            text=self._item_text(text),
            sort_order=self._next_item_order(checklist.id),
        )
        self.db.add(item)
        self.db.flush()
        return item

    def update_item(
        self, *, customer_id: int, checklist_id: int, item_id: int, text: str
    ) -> CustomerChecklistItem:
        item = self._item_or_error(customer_id, checklist_id, item_id)
        item.text = self._item_text(text)
        self.db.flush()
        return item

    def toggle_item(
        self, *, customer_id: int, checklist_id: int, item_id: int, actor: str
    ) -> CustomerChecklistItem:
        item = self._item_or_error(customer_id, checklist_id, item_id)
        item.is_completed = not item.is_completed
        item.completed_at = utcnow() if item.is_completed else None
        item.completed_by_username = actor[:64] if item.is_completed else None
        self.db.flush()
        return item

    def delete_item(self, *, customer_id: int, checklist_id: int, item_id: int) -> None:
        item = self._item_or_error(customer_id, checklist_id, item_id)
        self.db.delete(item)
        self.db.flush()

    def reorder_items(self, *, customer_id: int, checklist_id: int, ordered_ids: tuple[int, ...]) -> None:
        self._checklist_or_error(customer_id, checklist_id)
        items = tuple(
            self.db.scalars(
                select(CustomerChecklistItem)
                .where(CustomerChecklistItem.checklist_id == checklist_id)
                .order_by(CustomerChecklistItem.sort_order, CustomerChecklistItem.id)
            )
        )
        self._validate_order(ordered_ids, tuple(item.id for item in items), "Checklistenpunkte")
        by_id = {item.id: item for item in items}
        for index, item_id in enumerate(ordered_ids, start=1):
            by_id[item_id].sort_order = index
        self.db.flush()

    def _customer_or_error(self, customer_id: int) -> Customer:
        customer = self.db.get(Customer, customer_id)
        if customer is None:
            raise CustomerChecklistError("Der Kunde wurde nicht gefunden.")
        return customer

    def _checklist_or_error(self, customer_id: int, checklist_id: int) -> CustomerChecklist:
        checklist = self.db.scalar(
            select(CustomerChecklist).where(
                CustomerChecklist.id == checklist_id,
                CustomerChecklist.customer_id == customer_id,
            )
        )
        if checklist is None:
            raise CustomerChecklistError("Die Checkliste wurde nicht gefunden.")
        return checklist

    def _item_or_error(self, customer_id: int, checklist_id: int, item_id: int) -> CustomerChecklistItem:
        item = self.db.scalar(
            select(CustomerChecklistItem)
            .join(CustomerChecklist)
            .where(
                CustomerChecklistItem.id == item_id,
                CustomerChecklistItem.checklist_id == checklist_id,
                CustomerChecklist.customer_id == customer_id,
            )
        )
        if item is None:
            raise CustomerChecklistError("Der Checklistenpunkt wurde nicht gefunden.")
        return item

    def _next_checklist_order(self, customer_id: int) -> int:
        current = self.db.scalar(
            select(func.max(CustomerChecklist.sort_order)).where(CustomerChecklist.customer_id == customer_id)
        )
        return int(current or 0) + 1

    def _next_item_order(self, checklist_id: int) -> int:
        current = self.db.scalar(
            select(func.max(CustomerChecklistItem.sort_order)).where(
                CustomerChecklistItem.checklist_id == checklist_id
            )
        )
        return int(current or 0) + 1

    @staticmethod
    def _title(value: str) -> str:
        normalized = " ".join(value.split())
        if not 1 <= len(normalized) <= 120:
            raise CustomerChecklistError("Der Name der Checkliste muss zwischen 1 und 120 Zeichen lang sein.")
        return normalized

    @staticmethod
    def _item_text(value: str) -> str:
        normalized = " ".join(value.split())
        if not 1 <= len(normalized) <= 500:
            raise CustomerChecklistError("Der Checklistenpunkt muss zwischen 1 und 500 Zeichen lang sein.")
        return normalized

    @staticmethod
    def _validate_order(submitted: tuple[int, ...], stored: tuple[int, ...], label: str) -> None:
        if len(submitted) != len(set(submitted)) or set(submitted) != set(stored):
            raise CustomerChecklistError(f"Die Reihenfolge der {label} ist nicht mehr aktuell. Bitte lade die Seite neu.")

    @staticmethod
    def _view(checklist: CustomerChecklist) -> CustomerChecklistView:
        items = tuple(
            CustomerChecklistItemView(id=item.id, text=item.text, is_completed=item.is_completed)
            for item in sorted(checklist.items, key=lambda current: (current.sort_order, current.id))
        )
        return CustomerChecklistView(
            id=checklist.id,
            title=checklist.title,
            items=items,
            is_completed=bool(items) and all(item.is_completed for item in items),
        )
