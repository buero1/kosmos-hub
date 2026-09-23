import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.services.email_template_folders import EmailTemplateFolderError, EmailTemplateFolderService


def test_template_folders_can_be_seeded_created_and_deleted_when_empty():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = EmailTemplateFolderService(db=db)
        assert service.ensure_folders(("Kunden", "Leads", "Kunden")) == 2
        assert service.ensure_folders(("Kunden", "Leads")) == 0
        created = service.create_folder(name="  Intern neu  ")

        assert [folder.name for folder in service.list_folders()] == ["Kunden", "Leads", "Intern neu"]
        assert service.resolve_folder_name(name="intern NEU") == "Intern neu"
        assert service.delete_empty_folder(folder_id=created.id, used_folder_names={"Kunden"}) == "Intern neu"
        assert [folder.name for folder in service.list_folders()] == ["Kunden", "Leads"]


def test_template_folders_reject_duplicates_and_deleting_used_folders():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = EmailTemplateFolderService(db=db)
        service.ensure_folders(("Kunden",))
        folder = service.list_folders()[0]

        with pytest.raises(EmailTemplateFolderError, match="bereits vorhanden"):
            service.create_folder(name="kunden")
        with pytest.raises(EmailTemplateFolderError, match="nicht gefunden"):
            service.resolve_folder_name(name="Unbekannt")
        with pytest.raises(EmailTemplateFolderError, match="Nur leere"):
            service.delete_empty_folder(folder_id=folder.id, used_folder_names={"KUNDEN"})
