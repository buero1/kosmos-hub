from types import SimpleNamespace
import json
from urllib.parse import parse_qs, urlsplit

from app.api.routes import web


class FakeDb:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_bulk_email_template_routes_move_and_delete_selected_templates(monkeypatch):
    calls: list[tuple[str, object]] = []
    service = SimpleNamespace(execute=lambda name, values: calls.append((name, values))
        or SimpleNamespace(outputs={"changed_count": "2", "folder_name": "Intern"}))
    monkeypatch.setattr(web, "require_csrf", lambda request, token: calls.append(("csrf", token)))
    monkeypatch.setattr(web, "_require_hub_admin", lambda request: SimpleNamespace(username="kosmosadmin"))
    monkeypatch.setattr(web, "HubOperationService", lambda **kwargs: service)
    monkeypatch.setattr(
        web,
        "EmailTemplateFolderService",
        lambda db: SimpleNamespace(resolve_folder_name=lambda **kwargs: "Intern"),
    )
    monkeypatch.setattr(web, "write_audit_log", lambda db, **kwargs: calls.append(("audit", kwargs)))
    db = FakeDb()

    move_response = web.move_email_templates(
        request=SimpleNamespace(),
        db=db,
        template_ids=["template-1", "template-2"],
        folder_name="intern",
        csrf_token="csrf-token",
    )
    delete_response = web.delete_email_templates(
        request=SimpleNamespace(),
        db=db,
        template_ids=["template-1", "template-2"],
        return_folder="Kunden",
        confirmation="confirmed",
        csrf_token="csrf-token",
    )

    move_query = parse_qs(urlsplit(move_response.headers["location"]).query)
    delete_query = parse_qs(urlsplit(delete_response.headers["location"]).query)
    assert move_response.status_code == 303
    assert delete_response.status_code == 303
    assert move_query["folder"] == ["Intern"]
    assert "2 Vorlagen wurden" in move_query["message"][0]
    assert delete_query["folder"] == ["Kunden"]
    assert "2 Vorlagen wurden" in delete_query["message"][0]
    assert ("emails.templates.move", {"template_ids": json.dumps(["template-1", "template-2"]), "folder_name": "intern"}) in calls
    assert ("emails.templates.delete", {"template_ids": json.dumps(["template-1", "template-2"])}) in calls
    assert db.commits == 2
    assert db.rollbacks == 0


def test_bulk_email_template_delete_requires_explicit_confirmation(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(web, "require_csrf", lambda request, token: None)
    monkeypatch.setattr(web, "_require_hub_admin", lambda request: SimpleNamespace(username="kosmosadmin"))
    monkeypatch.setattr(
        web,
        "HubOperationService",
        lambda **kwargs: SimpleNamespace(execute=lambda *args: calls.append(args)),
    )
    db = FakeDb()

    response = web.delete_email_templates(
        request=SimpleNamespace(),
        db=db,
        template_ids=["template-1"],
        return_folder="Kunden",
        confirmation="",
        csrf_token="csrf-token",
    )

    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert response.status_code == 303
    assert query["state"] == ["error"]
    assert calls == []
    assert db.commits == 0


def test_updating_email_template_preserves_library_return_state(monkeypatch):
    calls: list[tuple[str, object]] = []
    service = SimpleNamespace(execute=lambda name, values: calls.append((name, values))
        or SimpleNamespace(outputs={"template_id": "template-1"}))
    monkeypatch.setattr(web, "require_csrf", lambda request, token: calls.append(("csrf", token)))
    monkeypatch.setattr(web, "_require_hub_admin", lambda request: SimpleNamespace(username="kosmosadmin"))
    monkeypatch.setattr(web, "HubOperationService", lambda **kwargs: service)
    monkeypatch.setattr(web, "write_audit_log", lambda db, **kwargs: calls.append(("audit", kwargs)))
    db = FakeDb()

    response = web.update_email_template(
        template_id="template-1",
        request=SimpleNamespace(),
        db=db,
        name="Angebot senden",
        subject="Ihr Angebot",
        content="<p>Inhalt</p>",
        context_module="customers",
        folder_name="Kunden Hub",
        return_folder="Kunden Hub",
        return_search="angebot",
        return_scroll="321",
        return_focus="template-1",
        csrf_token="csrf-token",
    )

    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert response.status_code == 303
    assert query["folder"] == ["Kunden Hub"]
    assert query["search"] == ["angebot"]
    assert query["scroll"] == ["321"]
    assert query["focus"] == ["template-1"]
    assert db.commits == 1
    assert db.rollbacks == 0


def test_email_template_return_state_normalizes_scroll_values():
    assert web._email_template_return_state(
        folder=" Kunden Hub ",
        search=" angebot ",
        scroll="-20",
        focus="template-1",
    ) == {
        "folder": "Kunden Hub",
        "search": "angebot",
        "scroll": "0",
        "focus": "template-1",
    }
    assert "scroll" not in web._email_template_return_state(scroll="kein-wert")


def test_translated_template_names_require_a_standard_hub_clone_in_destination_folder():
    templates = (
        SimpleNamespace(
            id="standard",
            name="Standard_hub",
            category="Kunden Hub",
            cloned_from="zoho-standard",
        ),
        SimpleNamespace(
            id="translated",
            name="  Auftragsbestätigung ",
            category="Kunden Hub",
            cloned_from="standard",
        ),
        SimpleNamespace(
            id="unrelated-copy",
            name="Nur kopiert",
            category="Kunden Hub",
            cloned_from="zoho-template",
        ),
        SimpleNamespace(
            id="lead-standard",
            name="Standard_hub",
            category="Leads Hub",
            cloned_from="zoho-lead-standard",
        ),
        SimpleNamespace(
            id="translated-lead",
            name="Terminbestätigung",
            category="Leads Hub",
            cloned_from="lead-standard",
        ),
    )

    assert web._translated_email_template_names(
        templates,
        destination_folder="Kunden Hub",
    ) == frozenset({"auftragsbestätigung"})
    assert web._translated_email_template_names(
        templates,
        destination_folder="Leads Hub",
    ) == frozenset({"terminbestätigung"})


def test_email_template_page_marks_reviewed_leads_hub_template(monkeypatch):
    template = SimpleNamespace(
        id="lead-template",
        name="Terminbestätigung",
        subject="Ihr Termin",
        module="Leads",
        category="Leads Hub",
        compiler_mode="legacy",
        context_module="leads",
        cloned_from="lead-standard",
        content_reviewed=True,
    )
    service = SimpleNamespace(list_email_templates=lambda: (template,))
    folder_service = SimpleNamespace(
        ensure_folders=lambda names: False,
        list_folders=lambda: (),
    )
    monkeypatch.setattr(web, "_require_hub_admin", lambda request: SimpleNamespace(username="admin"))
    monkeypatch.setattr(web, "_customer_communication_service", lambda db: service)
    monkeypatch.setattr(web, "template_library", lambda gateway: (template,))
    monkeypatch.setattr(web, "EmailTemplateFolderService", lambda db: folder_service)
    monkeypatch.setattr(
        web.templates,
        "TemplateResponse",
        lambda request, name, context: context,
    )

    context = web.email_template_management_page(
        request=SimpleNamespace(session={}, state=SimpleNamespace(hub_user=SimpleNamespace(username="admin"))),
        db=FakeDb(),
    )

    assert context["template_library_templates"][0]["is_reviewed"] is True
