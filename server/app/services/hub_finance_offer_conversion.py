"""Copy offer positions into an independent, privately owned order draft."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.models.hub_finance_documents import HubFinanceOrder, HubFinanceOrderLine
from app.models.hub_finance_offer import HubFinanceOffer, HubFinanceOfferLine
from app.services.hub_finance import HubFinanceService
from app.services.hub_finance_documents import HubFinanceDocumentError, HubFinanceDocumentService


def convert_offer_to_order(*, db, cipher, offer_id: int, owner_user_id: int) -> HubFinanceOrder:
    # Keep a savepoint rollback-safe with sqlite3's legacy transaction mode too.
    connection = db.connection()
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")
    with db.begin_nested():
        source = db.scalar(
            select(HubFinanceOffer).where(HubFinanceOffer.id == offer_id)
            .with_for_update().execution_options(populate_existing=True)
        )
        if source is None:
            raise HubFinanceDocumentError("Das Angebot wurde nicht gefunden.")
        existing = db.scalar(select(HubFinanceOrder).where(
            HubFinanceOrder.offer_id == source.id,
            HubFinanceOrder.unassigned_owner_user_id == owner_user_id,
        ).order_by(HubFinanceOrder.id).limit(1).with_for_update())
        if existing is not None:
            return existing

        # Current reads avoid stale snapshots after waiting for a concurrent offer edit.
        lines = db.scalars(select(HubFinanceOfferLine).where(HubFinanceOfferLine.offer_id == source.id)
                           .order_by(HubFinanceOfferLine.position_index)
                           .with_for_update().execution_options(populate_existing=True)).all()

        finance = HubFinanceService(db=db, cipher=cipher)
        documents = HubFinanceDocumentService(db=db, cipher=cipher)
        source_values = finance._values(source.encrypted_fields_json)
        now = datetime.now(UTC).isoformat()
        order = HubFinanceOrder(
            offer_id=source.id, unassigned_owner_user_id=owner_user_id,
            encrypted_fields_json=documents._encrypt({
                "status": "draft", "currency": source_values.get("currency") or "EUR",
                "created_time": now, "modified_time": now,
            }),
        )
        # Copy snapshots, not live article defaults or formatted display values.
        order.lines = [HubFinanceOrderLine(
            article_id=line.article_id, position_index=line.position_index,
            encrypted_fields_json=line.encrypted_fields_json,
        ) for line in lines]
        db.add(order)
        db.flush()
        order.order_number = f"AUF-{order.id:06d}"
        db.flush()
        return order
