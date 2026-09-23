"""Versioned administration of terms and conditions used by Finance PDFs."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.hub_legal_terms import HubLegalTerms, HubLegalTermsRevision
from app.models.hub_pdf_template import HubPdfTemplate, HubPdfTemplateRevision
from app.models.hub_user import HubUser
from app.services.customer_communications import CustomerCommunicationService
from app.services.hub_document_template_catalog import NAME_FIELD, NAME_MIN_LENGTH


class HubLegalTermsError(ValueError):
    """A safe validation message for the AGB editor."""


class HubLegalTermsService:
    """Maintain AGB documents and immutable revisions."""

    DEFAULT_NAME = "Standard-AGB"
    DEFAULT_CONTENT = (
        "<h2>Allgemeine Geschäftsbedingungen (AGB)</h2>"
        "<p>Bitte hinterlegen Sie hier Ihre Allgemeinen Geschäftsbedingungen.</p>"
    )

    def __init__(self, *, db: Session):
        self.db = db

    def ensure_default(self) -> HubLegalTerms:
        existing = self.db.scalar(
            select(HubLegalTerms)
            .where(HubLegalTerms.is_archived.is_(False))
            .order_by(HubLegalTerms.id.asc())
        )
        if existing is not None:
            return existing
        legal_terms = HubLegalTerms(
            name=self.DEFAULT_NAME,
            version=1,
            content_html=self._sanitize_html(self.DEFAULT_CONTENT),
            is_archived=False,
            created_by_username="system",
        )
        self.db.add(legal_terms)
        self.db.flush()
        self._add_revision(legal_terms=legal_terms, actor_username="system")
        return legal_terms

    def list_terms(self, *, initialize: bool = True) -> tuple[HubLegalTerms, ...]:
        if initialize:
            self.ensure_default()
        return tuple(
            self.db.scalars(
                select(HubLegalTerms)
                .where(HubLegalTerms.is_archived.is_(False))
                .order_by(HubLegalTerms.name.asc(), HubLegalTerms.id.asc())
            ).all()
        )

    def get(self, legal_terms_id: int) -> HubLegalTerms | None:
        legal_terms = self.db.get(HubLegalTerms, legal_terms_id)
        if legal_terms is None or legal_terms.is_archived:
            return None
        return legal_terms

    def revision_for(self, legal_terms: HubLegalTerms) -> HubLegalTermsRevision:
        revision = self.db.scalar(
            select(HubLegalTermsRevision).where(
                HubLegalTermsRevision.legal_terms_id == legal_terms.id,
                HubLegalTermsRevision.version == legal_terms.version,
            )
        )
        if revision is None:
            raise HubLegalTermsError("Die aktuelle AGB-Version wurde nicht gefunden.")
        return revision

    def create(self, *, actor: HubUser, name: str) -> HubLegalTerms:
        self._require_admin(actor)
        legal_terms = HubLegalTerms(
            name=self._name(name),
            version=1,
            content_html=self._sanitize_html(self.DEFAULT_CONTENT),
            is_archived=False,
            created_by_username=actor.username,
        )
        self.db.add(legal_terms)
        self.db.flush()
        self._add_revision(legal_terms=legal_terms, actor_username=actor.username)
        return legal_terms

    def update(
        self,
        *,
        actor: HubUser,
        legal_terms_id: int,
        name: str,
        content_html: str,
    ) -> HubLegalTerms:
        legal_terms = self._required(actor=actor, legal_terms_id=legal_terms_id)
        normalized_name = self._name(name)
        normalized_html = self._sanitize_html(content_html)
        if normalized_name == legal_terms.name and normalized_html == legal_terms.content_html:
            return legal_terms
        legal_terms.name = normalized_name
        legal_terms.content_html = normalized_html
        legal_terms.version += 1
        self.db.flush()
        revision = self._add_revision(legal_terms=legal_terms, actor_username=actor.username)
        self._version_linked_templates(actor_username=actor.username, legal_terms_revision=revision)
        return legal_terms

    def duplicate(self, *, actor: HubUser, legal_terms_id: int) -> HubLegalTerms:
        source = self._required(actor=actor, legal_terms_id=legal_terms_id)
        duplicate = HubLegalTerms(
            name=self._copy_name(source.name),
            version=1,
            content_html=source.content_html,
            is_archived=False,
            created_by_username=actor.username,
        )
        self.db.add(duplicate)
        self.db.flush()
        self._add_revision(legal_terms=duplicate, actor_username=actor.username)
        return duplicate

    def delete(self, *, actor: HubUser, legal_terms_id: int) -> None:
        legal_terms = self._required(actor=actor, legal_terms_id=legal_terms_id)
        linked_template = self.db.scalar(
            select(HubPdfTemplate.id).where(HubPdfTemplate.legal_terms_id == legal_terms.id).limit(1)
        )
        if linked_template is not None:
            raise HubLegalTermsError(
                "Diese AGB wird von einer PDF-Vorlage verwendet und kann noch nicht gelöscht werden."
            )
        alternatives = [item for item in self.list_terms() if item.id != legal_terms.id]
        if not alternatives:
            raise HubLegalTermsError("Die letzte AGB kann nicht gelöscht werden.")
        legal_terms.is_archived = True
        self.db.flush()

    def _required(self, *, actor: HubUser, legal_terms_id: int) -> HubLegalTerms:
        self._require_admin(actor)
        legal_terms = self.get(legal_terms_id)
        if legal_terms is None:
            raise HubLegalTermsError("Die AGB wurde nicht gefunden.")
        return legal_terms

    def _add_revision(self, *, legal_terms: HubLegalTerms, actor_username: str) -> HubLegalTermsRevision:
        revision = HubLegalTermsRevision(
            legal_terms_id=legal_terms.id,
            version=legal_terms.version,
            name=legal_terms.name,
            content_html=legal_terms.content_html,
            created_by_username=actor_username,
        )
        self.db.add(revision)
        self.db.flush()
        return revision

    def _version_linked_templates(
        self,
        *,
        actor_username: str,
        legal_terms_revision: HubLegalTermsRevision,
    ) -> None:
        templates = self.db.scalars(
            select(HubPdfTemplate).where(HubPdfTemplate.legal_terms_id == legal_terms_revision.legal_terms_id)
        ).all()
        for template in templates:
            template.version += 1
            self.db.flush()
            self.db.add(
                HubPdfTemplateRevision(
                    template_id=template.id,
                    version=template.version,
                    name=template.name,
                    content_json=template.content_json,
                    legal_terms_revision_id=legal_terms_revision.id,
                    created_by_username=actor_username,
                )
            )
        self.db.flush()

    def _copy_name(self, source_name: str) -> str:
        existing = {item.name.casefold() for item in self.list_terms()}
        candidate = f"{source_name} (Kopie)"[:255]
        index = 2
        while candidate.casefold() in existing:
            suffix = f" (Kopie {index})"
            candidate = f"{source_name[:255 - len(suffix)]}{suffix}"
            index += 1
        return candidate

    @staticmethod
    def _require_admin(actor: HubUser) -> None:
        if actor.role != "admin":
            raise HubLegalTermsError("Nur Hub-Administratoren können AGBs ändern.")

    @staticmethod
    def _name(value: str) -> str:
        normalized = " ".join(value.split())
        if not NAME_MIN_LENGTH <= len(normalized) <= NAME_FIELD.max_length:
            raise HubLegalTermsError("Der AGB-Name muss zwischen 3 und 255 Zeichen lang sein.")
        return normalized

    @staticmethod
    def _sanitize_html(value: str) -> str:
        try:
            return CustomerCommunicationService._sanitized_email_content(
                value.strip(),
                allow_template_href_placeholders=False,
            )
        except ValueError as exc:
            raise HubLegalTermsError(str(exc).replace("Nachricht", "AGB-Inhalt")) from exc
