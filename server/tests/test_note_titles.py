import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.core.templates import create_templates
from app.db.base import Base
from app.models.hub_lead import HubLead
from app.services.hub_lead_notes import HubLeadNoteService
from app.services.hub_note_catalog import normalize_note


@pytest.mark.parametrize("creating", [False, True])
@pytest.mark.parametrize("title", ["", " \n "])
def test_title_is_optional_for_create_and_edit(creating, title):
    assert normalize_note(title=title, content=" First line\nSecond line ", creating=creating) == {
        "title": "", "content": "First line\nSecond line",
    }


@pytest.mark.parametrize("creating", [False, True])
def test_optional_title_does_not_relax_content_or_length_validation(creating):
    with pytest.raises(ValueError, match="Notiz darf nicht leer"):
        normalize_note(title="", content="  ", creating=creating)
    with pytest.raises(ValueError, match="Titel"):
        normalize_note(title="x" * 256, content="Body", creating=creating)
    assert normalize_note(title=" Explicit title ", content="Body", creating=creating)["title"] == "Explicit title"


@pytest.mark.parametrize("template_name,collection", [
    ("customer_detail.html", "communication.notes"), ("lead_detail.html", "lead_notes"),
])
@pytest.mark.parametrize("title", ["", "Existing heading"])
def test_note_entries_render_only_explicit_titles(template_name, collection, title):
    source = Path("app/templates", template_name).read_text(encoding="utf-8")
    entry = source.split("{% for note in " + collection + " %}", 1)[1].split("{% else %}", 1)[0]
    note = SimpleNamespace(id=1, title=title, content="Unique body text", author="Author", occurred_at=None, last_error=None)
    env = create_templates(directory="app/templates").env
    html = env.from_string(entry).render(
        note=note, can_manage_communications=True,
        customer_agent_context_action=lambda *args: "", customer_note_entry_action=lambda *args: "",
        lead_note_entry_action=lambda *args: "",
    )
    assert html.count("Unique body text") == 1
    assert "Author" in html and "Ohne Titel" not in html
    assert ("<strong>Existing heading</strong>" in html) == bool(title)
    if not title:
        assert "<strong>" not in html
    title_input = re.search(r'<input name="title"[^>]+>', source).group(0)
    rendered_input = env.from_string(title_input).render()
    assert "required" not in rendered_input


def test_external_lead_notes_do_not_invent_titles():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        lead = HubLead(encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"company": "Example"}})))
        db.add(lead)
        db.flush()
        service = HubLeadNoteService(db=db, cipher=cipher)
        values = dict(lead_id=lead.id, source_system="test", source_external_id="1", actor="test", title="")
        created = service.upsert_external_note(**values, content="First note")
        updated = service.upsert_external_note(**values, content="Changed note")
        assert created.id == updated.id
        assert created.title == updated.title == ""
        assert updated.content == "Changed note"
    engine.dispose()
