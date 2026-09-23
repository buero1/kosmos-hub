from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.hub_email_template_folder import HubEmailTemplateFolder


class EmailTemplateFolderError(ValueError):
    """Raised when a template folder operation is invalid."""


class EmailTemplateFolderService:
    def __init__(self, *, db: Session) -> None:
        self.db = db

    def list_folders(self) -> tuple[HubEmailTemplateFolder, ...]:
        return tuple(self.db.scalars(
            select(HubEmailTemplateFolder).order_by(HubEmailTemplateFolder.id.asc())
        ).all())

    def ensure_folders(self, names: tuple[str, ...]) -> int:
        """Persist categories discovered in existing templates, preserving their order."""
        existing = {folder.name.casefold() for folder in self.list_folders()}
        created = 0
        for raw_name in names:
            name = self._name(raw_name)
            if name.casefold() in existing:
                continue
            self.db.add(HubEmailTemplateFolder(name=name))
            existing.add(name.casefold())
            created += 1
        if created:
            self.db.flush()
        return created

    def create_folder(self, *, name: str) -> HubEmailTemplateFolder:
        normalized_name = self._name(name)
        if any(folder.name.casefold() == normalized_name.casefold() for folder in self.list_folders()):
            raise EmailTemplateFolderError("Dieser Vorlagenordner ist bereits vorhanden.")
        folder = HubEmailTemplateFolder(name=normalized_name)
        self.db.add(folder)
        self.db.flush()
        return folder

    def resolve_folder_name(self, *, name: str) -> str:
        """Return the persisted spelling for an existing folder name."""
        normalized_name = self._name(name)
        folder = next(
            (
                item
                for item in self.list_folders()
                if item.name.casefold() == normalized_name.casefold()
            ),
            None,
        )
        if folder is None:
            raise EmailTemplateFolderError("Der Vorlagenordner wurde nicht gefunden.")
        return folder.name

    def delete_empty_folder(self, *, folder_id: int, used_folder_names: set[str]) -> str:
        folder = self.db.get(HubEmailTemplateFolder, folder_id)
        if folder is None:
            raise EmailTemplateFolderError("Der Vorlagenordner wurde nicht gefunden.")
        used_names = {name.strip().casefold() for name in used_folder_names if name.strip()}
        if folder.name.casefold() in used_names:
            raise EmailTemplateFolderError("Nur leere Vorlagenordner können gelöscht werden.")
        name = folder.name
        self.db.delete(folder)
        self.db.flush()
        return name

    @staticmethod
    def _name(value: str) -> str:
        name = " ".join(value.split())
        if not name:
            raise EmailTemplateFolderError("Gib einen Namen für den Vorlagenordner ein.")
        if len(name) > 255:
            raise EmailTemplateFolderError("Der Ordnername darf höchstens 255 Zeichen enthalten.")
        return name
