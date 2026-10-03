"""Repair customer relations for the one-time Zoho Books invoice import."""

from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import app.db.base  # noqa: F401 - register all SQLAlchemy relationships
from sqlalchemy import func, select

from app.core.security import get_secret_cipher
from app.db.session import SessionLocal
from app.models.customer import Customer
from app.models.hub_finance_documents import HubFinanceInvoice


EXPECTED_UNASSIGNED = 250
EXPECTED_REASON_COUNTS = {
    "exact-address": 198,
    "pdf-test-customer": 37,
    "pdf-gartengestaltung-berisha": 3,
    "pdf-el-camino-torsten-wilhelm": 12,
}
SPECIAL_ADDRESS_TARGETS = {
    "6649 N Blue Gum St\nNew Orleans\nLA\nOrleans": (
        "2930984000005565347",
        "pdf-test-customer",
    ),
    "Schaffhauser Straße 10\n81476 München": (
        "2930984000000345797",
        "pdf-gartengestaltung-berisha",
    ),
    "Bajuwarenstraße 5\n85757 Dachau": (
        "2930984000001656100",
        "pdf-el-camino-torsten-wilhelm",
    ),
}


def _normalize(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.replace("strasse", "str")
    return re.sub(r"[^a-z0-9]+", "", text)


def _decrypt_json(cipher, value: str | None) -> dict[str, object]:
    if not value:
        return {}
    try:
        payload = json.loads(cipher.decrypt(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _profile_fields(payload: dict[str, object]) -> dict[str, object]:
    fields = payload.get("fields", payload)
    return fields if isinstance(fields, dict) else {}


def _customer_addresses(db, cipher) -> tuple[list[dict[str, object]], dict[str, Customer]]:
    addresses: list[dict[str, object]] = []
    by_zoho_id: dict[str, Customer] = {}
    for customer in db.scalars(select(Customer)).all():
        fields = _profile_fields(_decrypt_json(cipher, customer.encrypted_profile_json))
        addresses.append(
            {
                "customer": customer,
                "street": _normalize(fields.get("Rechnungsadresse - Straße")),
                "postal_code": _normalize(fields.get("Rechnungsadresse - PLZ")),
                "city": _normalize(fields.get("Rechnungsadresse - Stadt")),
            }
        )
        if customer.zoho_id:
            if customer.zoho_id in by_zoho_id:
                raise RuntimeError(f"Duplicate customer Zoho ID: {customer.zoho_id}")
            by_zoho_id[customer.zoho_id] = customer
    return addresses, by_zoho_id


def _exact_address_candidates(
    normalized_billing_address: str,
    customer_addresses: list[dict[str, object]],
) -> list[Customer]:
    candidates: list[Customer] = []
    for entry in customer_addresses:
        street = str(entry["street"])
        postal_code = str(entry["postal_code"])
        city = str(entry["city"])
        if street and postal_code and city and all(
            part in normalized_billing_address for part in (street, postal_code, city)
        ):
            candidates.append(entry["customer"])
    return candidates


def _repair_plan(db, cipher, *, lock: bool) -> list[dict[str, object]]:
    statement = (
        select(HubFinanceInvoice)
        .where(
            HubFinanceInvoice.zoho_books_id.is_not(None),
            HubFinanceInvoice.customer_id.is_(None),
        )
        .order_by(HubFinanceInvoice.id)
    )
    if lock:
        statement = statement.with_for_update()
    invoices = list(db.scalars(statement).all())
    if len(invoices) != EXPECTED_UNASSIGNED:
        raise RuntimeError(
            f"Expected {EXPECTED_UNASSIGNED} unassigned imported invoices, found {len(invoices)}."
        )

    customer_addresses, customers_by_zoho_id = _customer_addresses(db, cipher)
    special_targets = {
        _normalize(address): (customers_by_zoho_id.get(zoho_id), reason)
        for address, (zoho_id, reason) in SPECIAL_ADDRESS_TARGETS.items()
    }
    if any(customer is None for customer, _reason in special_targets.values()):
        raise RuntimeError("At least one reviewed special-case customer no longer exists.")

    plan: list[dict[str, object]] = []
    for invoice in invoices:
        values = _decrypt_json(cipher, invoice.encrypted_fields_json)
        billing_address = str(values.get("billing_address") or "").strip()
        normalized_address = _normalize(billing_address)
        candidates = _exact_address_candidates(normalized_address, customer_addresses)
        if len(candidates) == 1:
            customer = candidates[0]
            reason = "exact-address"
        elif normalized_address in special_targets:
            customer, reason = special_targets[normalized_address]
            assert customer is not None
        else:
            raise RuntimeError(
                f"Invoice {invoice.invoice_number or invoice.id} has {len(candidates)} address candidates "
                "and is not an audited special case."
            )
        plan.append(
            {
                "invoice": invoice,
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "zoho_books_id": invoice.zoho_books_id,
                "old_customer_id": invoice.customer_id,
                "old_contact_id": invoice.contact_id,
                "new_customer_id": customer.id,
                "new_customer_name": customer.name,
                "new_customer_visible": customer.is_visible,
                "reason": reason,
            }
        )

    reason_counts = Counter(str(item["reason"]) for item in plan)
    if dict(reason_counts) != EXPECTED_REASON_COUNTS:
        raise RuntimeError(
            f"Unexpected mapping counts: {dict(reason_counts)}; expected {EXPECTED_REASON_COUNTS}."
        )
    return plan


def _summary(plan: list[dict[str, object]]) -> dict[str, object]:
    by_customer: dict[tuple[int, str, bool], list[dict[str, object]]] = defaultdict(list)
    for item in plan:
        key = (
            int(item["new_customer_id"]),
            str(item["new_customer_name"]),
            bool(item["new_customer_visible"]),
        )
        by_customer[key].append(item)
    customers = []
    for (customer_id, name, visible), items in sorted(by_customer.items(), key=lambda entry: entry[0][1].casefold()):
        numbers = sorted(str(item["invoice_number"] or "") for item in items)
        customers.append(
            {
                "customer_id": customer_id,
                "customer_name": name,
                "customer_visible": visible,
                "invoice_count": len(items),
                "first_invoice": numbers[0],
                "last_invoice": numbers[-1],
            }
        )
    return {
        "invoice_count": len(plan),
        "reason_counts": dict(Counter(str(item["reason"]) for item in plan)),
        "visible_customer_invoices": sum(bool(item["new_customer_visible"]) for item in plan),
        "hidden_customer_invoices": sum(not bool(item["new_customer_visible"]) for item in plan),
        "customers": customers,
    }


def _write_backup(plan: list[dict[str, object]], backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = backup_dir / f"zoho-invoice-customer-relations-{timestamp}.json"
    payload = {
        "created_at": datetime.now(UTC).isoformat(),
        "rows": [
            {
                key: item[key]
                for key in (
                    "invoice_id",
                    "invoice_number",
                    "zoho_books_id",
                    "old_customer_id",
                    "old_contact_id",
                    "new_customer_id",
                    "reason",
                )
            }
            for item in plan
        ],
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path, default=Path("/opt/kosmos-hub/backups"))
    args = parser.parse_args()

    cipher = get_secret_cipher()
    with SessionLocal() as db:
        plan = _repair_plan(db, cipher, lock=args.apply)
        result = _summary(plan)
        result["mode"] = "apply" if args.apply else "dry-run"
        if not args.apply:
            db.rollback()
            print(json.dumps(result, ensure_ascii=False))
            return

        backup_path = _write_backup(plan, args.backup_dir)
        for item in plan:
            invoice = item["invoice"]
            assert isinstance(invoice, HubFinanceInvoice)
            invoice.customer_id = int(item["new_customer_id"])
        db.commit()
        remaining = db.scalar(
            select(func.count())
            .select_from(HubFinanceInvoice)
            .where(
                HubFinanceInvoice.zoho_books_id.is_not(None),
                HubFinanceInvoice.customer_id.is_(None),
            )
        )
        if remaining != 0:
            raise RuntimeError(f"Repair committed but {remaining} imported invoices remain unassigned.")
        result["backup_path"] = str(backup_path)
        result["remaining_unassigned"] = remaining
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
