"""Offer operations shared by the finance UI and the Hub-Agent."""

from __future__ import annotations

from typing import Mapping

from sqlalchemy import select

from app.models.customer import Customer
from app.models.hub_finance_offer import HubFinanceOffer
from app.models.hub_user import HubUser
from app.services.customer_directory import CustomerDirectoryService
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_finance import HubFinanceService
from app.services.hub_finance_field_catalog import FINANCE_POSITION_UNITS, OFFER_FIELDS
from app.services.hub_finance_operations_shared import merge_form, require_record
from app.services.hub_finance_pdf_readers import load_pdf
from app.services.hub_finance_pdf_generation import HubFinancePdfService
from app.services.hub_leads import HubLeadService
from app.services.hub_record_access import identifier, require_actor
from app.services.hub_operations import (
    HubArtifact, HubOperation, HubOperationError, HubOperationInputField, HubOperationResult,
    HubOperationService, register_artifact, register_operation,
)


def _optional_id(value: str, label: str) -> int | None:
    raw = value.strip()
    if not raw:
        return None
    if not raw.isdecimal() or int(raw) < 1:
        raise HubOperationError(f"{label} ist ungültig.")
    return int(raw)


def _party_id(
    service: HubOperationService, *, values: Mapping[str, str], kind: str,
    user: HubUser, access: HubAccessControlService,
) -> int | None:
    record_id = _optional_id(values.get(f"{kind}_id", ""), "Verknüpfung")
    name = values.get(f"{kind}_name", "").strip()
    module_key = "leads" if kind == "lead" else "customers"
    if record_id is not None:
        if not access.can_access_record(user=user, module_key=module_key, record_id=record_id):
            raise HubOperationError("Der verknüpfte Datensatz ist nicht verfügbar.")
        return record_id
    if not name:
        return None
    allowed = access.accessible_record_ids(user=user, module_key=module_key)
    if kind == "lead":
        entries = HubLeadService(db=service.db, cipher=service.cipher).list_leads(allowed_lead_ids=allowed)
        matches = {entry.lead.id for entry in entries if name.casefold() in {entry.name.casefold(), entry.company.casefold()}}
    else:
        query = select(Customer)
        if allowed is not None:
            query = query.where(Customer.id.in_(allowed))
        matches = {customer.id for customer in service.db.scalars(query) if customer.name.casefold() == name.casefold()}
    if len(matches) != 1:
        raise HubOperationError(f"{kind.title()} konnte nicht eindeutig zugeordnet werden.")
    return matches.pop()


def _create_offer(service: HubOperationService, values: Mapping[str, str]) -> HubOperationResult:
    user = service.db.scalar(select(HubUser).where(HubUser.username == service.actor, HubUser.is_active.is_(True)))
    if user is None:
        raise HubOperationError("Der Benutzer ist nicht mehr verfügbar.")
    access = HubAccessControlService(db=service.db)
    if not access.can(user, "finance", "view") or not access.can(user, "finance", "create"):
        raise HubOperationError("Angebote dürfen mit diesem Benutzer nicht angelegt werden.")
    customer_id = _party_id(service, values=values, kind="customer", user=user, access=access)
    lead_id = _party_id(service, values=values, kind="lead", user=user, access=access)
    contact_id = _optional_id(values.get("contact_id", ""), "Ansprechpartner")
    pdf_template_id = _optional_id(values.get("pdf_template_id", ""), "PDF-Vorlage")
    submitted_values = merge_form(service, "offers", values)
    offer = HubFinanceService(db=service.db, cipher=service.cipher).create_offer(
        customer_id=customer_id,
        contact_id=contact_id,
        lead_id=lead_id,
        submitted_values=submitted_values,
        pdf_template_id=pdf_template_id,
    )
    token = HubFinancePdfService(db=service.db, cipher=service.cipher).queue(
        document_type="offers", document_id=offer.id,
    )
    recipient_email, recipient_name = _offer_recipient(service, offer)
    return HubOperationResult(
        label="Angebot öffnen", href=f"/finance/offers/{offer.id}", record_id=offer.id,
        background_token=token,
        outputs={
            "artifact_ref": f"finance.offer.pdf:{offer.id}",
            "recipient_email": recipient_email,
            "recipient_name": recipient_name,
            "customer_id": str(customer_id or ""),
            "lead_id": str(lead_id or ""),
        },
    )


def _duplicate_offer(service: HubOperationService, values: Mapping[str, str]) -> HubOperationResult:
    user, access = require_actor(service, "finance", "create")
    if not access.can(user, "finance", "edit"):
        raise HubOperationError("Zum Bearbeiten der Kopie fehlt die Berechtigung.")
    if set(values) != {"record_id"} or not isinstance(values["record_id"], str):
        raise HubOperationError("Bitte nur die ID des zu duplizierenden Angebots angeben.")
    source = require_record(service, "offers", identifier(values["record_id"]))
    offer = HubFinanceService(db=service.db, cipher=service.cipher).duplicate_offer(offer_id=source.id, owner_user_id=user.id)
    return HubOperationResult("Kopie bearbeiten", f"/finance/offers/{offer.id}?edit=true", offer.id,
                              outputs={"offer_number": offer.offer_number, "customer_id": "", "lead_id": ""})


def _offer_recipient(service: HubOperationService, offer: HubFinanceOffer) -> tuple[str, str]:
    if getattr(offer, "lead_id", None) is not None:
        lead = HubLeadService(db=service.db, cipher=service.cipher).get_detail(lead_id=offer.lead_id)
        if lead is not None:
            email = next((field.value for field in lead.fields if field.key == "email" and field.value != "-"), "")
            return email, lead.name
    if offer.contact_id is not None:
        contact = next((entry.contact for entry in CustomerDirectoryService(
            db=service.db, cipher=service.cipher,
        ).list_contact_entries() if entry.contact.id == offer.contact_id), None)
        if contact is not None:
            return contact.email or "", contact.name
    return "", ""


def _discard_offer_copy(service: HubOperationService, values: Mapping[str, str]) -> HubOperationResult:
    user, _ = require_actor(service, "finance", "edit")
    if set(values) != {"record_id"} or not isinstance(values["record_id"], str):
        raise HubOperationError("Bitte nur die ID der zu verwerfenden Angebotskopie angeben.")
    copy = require_record(service, "offers", identifier(values["record_id"]), "edit")
    copy_id = copy.id
    source_id = HubFinanceService(db=service.db, cipher=service.cipher).discard_offer_copy(
        offer_id=copy_id, actor_user_id=user.id, is_admin=user.role == "admin", after_commit=service.after_commit,
    )
    href = "/finance/offers"
    if source_id is not None:
        try:
            source = require_record(service, "offers", source_id)
        except HubOperationError:
            pass
        else:
            href = f"/finance/offers/{source.id}"
    return HubOperationResult("Kopie verworfen", href, copy_id)


def _load_offer_pdf(service: HubOperationService, identifier: str) -> HubArtifact:
    offer_id = _optional_id(identifier, "Angebot")
    offer = require_record(service, "offers", offer_id)
    generated, content = load_pdf(service, "offers", offer.id, source="generated", wait=True)
    recipient_email, _ = _offer_recipient(service, offer)
    return HubArtifact(
        filename=generated.filename or f"Angebot-{offer.id}.pdf", content_type="application/pdf",
        content=content, recipient_email=recipient_email,
    )


def _offer_input_fields() -> tuple[HubOperationInputField, ...]:
    return tuple(
        HubOperationInputField(
            name=f"offer_field__{field.key}", label=field.label,
            required=field.required, options=field.options,
            encoding="HTML" if field.display_type == "HTML" else "",
        )
        for field in OFFER_FIELDS
        if not field.read_only and field.key not in {"customer", "contact"}
    )


def _offer_input_guide() -> str:
    return (
        "Genau eine Verknüpfung: lead_id oder eindeutiger lead_name; alternativ customer_id "
        "oder eindeutiger customer_name. Bei Kunden ist contact_id erforderlich. "
        "Datumsfelder im Format YYYY-MM-DD; fehlt Gültig bis, gilt ein Kalendermonat nach dem Angebotsdatum. "
        "Pro Position offer_line__0__name, "
        "offer_line__0__quantity, offer_line__0__unit (optional), offer_line__0__unit_price, "
        "offer_line__0__tax_rate (0, 7 oder 19), offer_line__0__discount_percent; "
        f"Einheiten, falls angegeben: {', '.join(FINANCE_POSITION_UNITS)}. "
        "Für weitere Positionen Index 1, 2 usw. nutzen. Niemals Preis oder Leistungsumfang erfinden."
    )


def _offer_defaults(values: Mapping[str, str]) -> dict[str, str]:
    defaults = HubFinanceService.new_offer_values(offer_date=values.get("offer_field__offer_date"))
    line_defaults = {
        key.rsplit("__", 1)[1]: value
        for key, value in defaults.items() if key.startswith("offer_line__0__")
    }
    indices = {
        int(parts[1]) for key in values
        if len(parts := key.split("__")) == 3 and parts[0] == "offer_line" and parts[1].isdigit()
    }
    for index in indices - {0}:
        defaults.update({f"offer_line__{index}__{key}": value for key, value in line_defaults.items()})
    return defaults


def _offer_preview(values: Mapping[str, str]) -> tuple[str, ...]:
    lines = [
        f"Status: {values.get('offer_field__status', '-')}",
        f"Angebotsdatum: {values.get('offer_field__offer_date', '-')}",
        f"Gültig bis: {values.get('offer_field__valid_until', '-')}",
        f"Währung: {values.get('offer_field__currency', '-')}",
    ]
    target = values.get("lead_name") or values.get("lead_id")
    if target:
        lines.append(f"Lead: {target}")
    target = values.get("customer_name") or values.get("customer_id")
    if target:
        lines.append(f"Kunde: {target}")
    if values.get("contact_id"):
        lines.append(f"Ansprechpartner-ID: {values['contact_id']}")
    for index in sorted({
        int(parts[1]) for key in values
        if len(parts := key.split("__")) == 3 and parts[0] == "offer_line" and parts[1].isdigit()
    }):
        prefix = f"offer_line__{index}__"
        name = values.get(f"{prefix}name", "").strip()
        if not name:
            continue
        quantity = values.get(f"{prefix}quantity", "?")
        price = values.get(f"{prefix}unit_price", "?")
        unit = values.get(f"{prefix}unit", "")
        unit_suffix = f" · {unit}" if unit else ""
        tax = values.get(f"{prefix}tax_rate", "?")
        lines.append(
            f"Position {index + 1}: {name} · {quantity} × {price} {values.get('offer_field__currency', 'EUR')} "
            f"netto{unit_suffix} · {tax} % MwSt."
        )
    lines.append("Die Angebots-PDF wird anschließend erzeugt; keine E-Mail wird versendet.")
    return tuple(lines)


register_operation(HubOperation(
    key="finance.offers.create",
    module="finance",
    label="Angebot anlegen",
    description="Erstellt ein Angebot für genau einen Kunden oder Lead und startet die PDF-Erzeugung.",
    input_guide=_offer_input_guide(),
    preview_fields=(("lead_name", "Lead"), ("lead_id", "Lead-ID"),
                    ("customer_name", "Kunde"), ("customer_id", "Kunden-ID"),
                    ("offer_field__reference", "Referenz"),
                    ("offer_field__valid_until", "Gültig bis")),
    execute=_create_offer,
    preview_builder=_offer_preview,
    defaults=_offer_defaults,
    input_fields=_offer_input_fields,
    result_fields=(
        ("artifact_ref", "Verweis auf die erzeugte Angebots-PDF, als attachment_ref nutzbar"),
        ("recipient_email", "E-Mail-Adresse des Angebotskontakts, falls hinterlegt"),
        ("recipient_name", "Name des Angebotskontakts"),
        ("customer_id", "Verknüpfter Kunde, sonst leer"),
        ("lead_id", "Verknüpfter Lead, sonst leer"),
    ),
))
register_artifact("finance.offer.pdf", _load_offer_pdf)

register_operation(HubOperation(
    key="finance.offers.duplicate", module="finance", label="Angebot duplizieren",
    description="Kopiert Angebotsdaten und Positionen in einen neuen Entwurf mit eigener ANG-Nummer. Kunde, Lead und Ansprechpartner werden geleert. Keine PDF oder E-Mail wird erzeugt oder uebernommen.",
    input_guide="record_id des lesbaren Quellangebots. Die Kopie ist bis zur Verknuepfung nur fuer Ersteller und Admin sichtbar. Datumswerte, Konditionen und Positionen bleiben erhalten; weitere Anpassungen ueber finance.offers.update.",
    input_fields=lambda: (HubOperationInputField("record_id", "Quellangebot-ID", required=True),),
    preview_fields=(("record_id", "Quellangebot-ID"),),
    execute=_duplicate_offer,
    result_fields=(("offer_number", "Neue Angebotsnummer"), ("customer_id", "Leer"), ("lead_id", "Leer")),
))

register_operation(HubOperation(
    key="finance.offers.discard_copy", module="finance", label="Angebotskopie verwerfen",
    description="Verwirft eine noch nicht erfolgreich gespeicherte, unverknuepfte Angebotskopie samt Positionen. Bereits gespeicherte Angebote bleiben geschuetzt.",
    input_guide="record_id der unfertigen Kopie. Nur der Ersteller oder ein Admin mit Finance-Bearbeitungsrecht darf sie verwerfen. Das Quellangebot bleibt unveraendert.",
    input_fields=lambda: (HubOperationInputField("record_id", "Angebotskopie-ID", required=True),),
    preview_fields=(("record_id", "Zu verwerfende Angebotskopie-ID"),),
    execute=_discard_offer_copy,
))
