"""Shared email preparation for the composer, templates and Hub operations."""

from dataclasses import replace
from html import escape
from html.parser import HTMLParser
import re

from app.core.config import get_settings
from app.models.customer_communication import CustomerZohoEmail
from app.services.customer_communications import CustomerCommunicationService
from app.services.email_composer_settings import EmailComposerSettingsService
from app.services.hub_mailbox import HubMailboxService
from app.services.hub_operations import HubOperationError
from app.services.hub_record_access import require_actor, identifier


_AGENT_REPLY_INLINE_TAGS = frozenset({"a", "b", "em", "i", "s", "strong", "sub", "sup", "u"})
_AGENT_REPLY_BLOCK_TAGS = frozenset({"div", "h1", "h2", "h3", "h4", "h5", "h6", "p", "pre"})
_AGENT_REPLY_CLOSING_SALUTATION_PATTERN = re.compile(
    r"(?:<br>\s*)?(?:mit\s+freundlichen|freundliche|viele|beste|herzliche|liebe)\s+gr(?:ü|ue)ße?\.?\s*$",
    flags=re.IGNORECASE,
)


class ReplyHtmlNormalizer(HTMLParser):
    """Convert the agent's safe HTML into the Hub's text-and-break email format."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.open_inline_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.casefold()
        if normalized == "br":
            self.parts.append("<br>")
            return
        if normalized == "li":
            if self.parts and not self.parts[-1].endswith("<br>"):
                self.parts.append("<br>")
            self.parts.append("• ")
            return
        if normalized not in _AGENT_REPLY_INLINE_TAGS:
            return
        if normalized == "a":
            href = next((value for name, value in attrs if name.casefold() == "href" and value), "")
            if not href:
                return
            self.parts.append(f'<a href="{escape(href, quote=True)}">')
        else:
            self.parts.append(f"<{normalized}>")
        self.open_inline_tags.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized == "li":
            self.parts.append("<br>")
            return
        if normalized in _AGENT_REPLY_BLOCK_TAGS:
            self.parts.append("<br><br>")
            return
        if normalized not in self.open_inline_tags:
            return
        while self.open_inline_tags:
            opened = self.open_inline_tags.pop()
            self.parts.append(f"</{opened}>")
            if opened == normalized:
                return

    def handle_data(self, data: str) -> None:
        self.parts.append(escape(data))

    def content(self) -> str:
        while self.open_inline_tags:
            self.parts.append(f"</{self.open_inline_tags.pop()}>")
        normalized = "".join(self.parts).strip()
        normalized = re.sub(r"(?:\s*<br>\s*){3,}", "<br><br>", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"(?:\s*<br>\s*)+$", "", normalized, flags=re.IGNORECASE)
        normalized = _AGENT_REPLY_CLOSING_SALUTATION_PATTERN.sub("", normalized).strip()
        return re.sub(r"(?:\s*<br>\s*)+$", "", normalized, flags=re.IGNORECASE)


def normalize_reply_html(db, value):
    parser = ReplyHtmlNormalizer()
    parser.feed(CustomerCommunicationService._sanitized_email_content(value))
    parser.close()
    normalized = parser.content()
    if not re.sub(r"<[^>]+>", "", normalized).strip():
        raise HubOperationError("Der Antwortentwurf enthaelt keinen lesbaren Text.")
    settings = EmailComposerSettingsService(db=db).get_runtime_settings()
    return f'<span style="{escape(settings.font_style, quote=True)}">{normalized}</span>'


def mailbox_for(service, account_id=None):
    return HubMailboxService(db=service.db, cipher=service.cipher, actor=service.actor, public_base_url=get_settings().public_base_url, account_id=account_id)


def compose_context(service, email_key, action, *, allow_fetch=True):
    require_actor(service, "emails", "view")
    if action not in {"reply", "reply_all", "forward"}:
        raise HubOperationError("Unbekannte E-Mail-Aktion.")
    mailbox = mailbox_for(service)
    source = mailbox.scope.require(email_key)
    if email_key.startswith("scheduled-") or getattr(source, "source", "") == "hub-draft":
        raise HubOperationError("Diese E-Mail ist noch nicht versendet.")
    if isinstance(source, CustomerZohoEmail):
        if action == "forward":
            prepared = mailbox.communications.get_email_forward(customer_id=source.customer_id, email_id=source.id, allow_fetch=allow_fetch)
            return {"action": action, "customer_id": source.customer_id, "recipient": None, "recipient_email": "",
                    "subject": prepared.subject, "content": prepared.content, "cc_emails": [],
                    "reply_to_email_id": None, "forward_from_email_id": source.id}
        prepared = mailbox.communications.get_email_reply(customer_id=source.customer_id, email_id=source.id, allow_fetch=allow_fetch)
        return {"action": action, "customer_id": source.customer_id,
                "recipient": {"key": prepared.recipient_key, "name": prepared.recipient_name, "email": prepared.recipient_email},
                "recipient_email": prepared.recipient_email, "subject": prepared.subject, "content": prepared.content,
                "cc_emails": list(prepared.reply_all_cc_emails) if action == "reply_all" else [],
                "reply_to_email_id": prepared.email_id, "forward_from_email_id": None}
    payload = mailbox._payload(source.encrypted_payload_json)
    if not mailbox._unassigned_content(payload):
        raise HubOperationError("Der Nachrichtentext ist nicht lokal gespeichert.")
    context = mailbox.get_unassigned_email_compose_context(email_id=source.id, action=action)
    context["lead_id"] = payload.get("recipient_lead_id") or payload.get("lead_id")
    # Unassigned source IDs belong to the mailbox, not the customer email table.
    return context


def forward_attachments(service, email_key):
    mailbox = mailbox_for(service)
    source = mailbox.scope.require(email_key)
    payload = mailbox._payload(source.encrypted_payload_json)
    entries = mailbox.communications._email_attachments(payload)
    if len(entries) > 20:
        raise HubOperationError("Es koennen hoechstens 20 Anhaenge uebernommen werden.")
    total = 0
    downloads = []
    for entry in entries:
        if isinstance(source, CustomerZohoEmail):
            item = mailbox.communications.download_email_attachment(customer_id=source.customer_id, email_id=source.id, attachment_id=entry.id)
        else:
            item = mailbox.download_unassigned_attachment(email_id=source.id, attachment_id=entry.id)
        total += len(item.content)
        if total > 50 * 1024 * 1024:
            raise HubOperationError("Die Anhaenge sind zusammen groesser als 50 MB.")
        downloads.append(item)
    return tuple(downloads)


def render_template(service, values):
    require_actor(service, "emails", "view")
    from app.services.hub_template_contexts import resolve_context
    inputs, tokens = resolve_context(service, values)
    communications = mailbox_for(service).communications
    customer_id = identifier(inputs.get("customer_id", ""))
    if customer_id is not None:
        recipient_key = inputs.get("recipient_key", "")
        email = inputs.get("recipient_email", "").strip().casefold()
        if recipient_key or email:
            recipients = communications.list_recipients(customer_id=customer_id)
            matching = [item for item in recipients if (not email or item.email.casefold() == email)
                        and (not recipient_key or item.key == recipient_key)]
            if len(matching) != 1:
                raise HubOperationError("Empfaenger und ausgewaehlter Vorlagenkontakt stimmen nicht ueberein.")
            inputs["recipient_key"] = matching[0].key
        detail = communications.get_email_template(customer_id=customer_id, template_id=inputs["template_id"],
            recipient_key=inputs.get("recipient_key", ""), template_values=tokens)
    else:
        detail = communications.get_email_template_preview(template_id=inputs["template_id"], template_values=tokens)
    return replace(detail, template_context={key: inputs[key] for key in
        ("customer_id", "lead_id", "dunning_id", "recipient_key", "context_module", "context_record_id") if inputs.get(key)})
