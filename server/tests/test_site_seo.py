import json

import pytest
from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_user import HubUser
from app.models.site import Site
from app.services.hub_operations import HubOperationService
from app.services.site_mcp_proxy import SiteMcpProxyService
from app.services.site_seo import SiteSeoError, SiteSeoService, prepare_apply
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def context():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    cipher = SecretCipher("site-seo-tests")
    with sessions() as db:
        db.add_all([
            HubUser(username="admin", role="admin", password_hash="hash"),
            Site(id=1, uuid="seo-site", domain="seo.example", home_url="https://seo.example",
                site_url="https://seo.example", status="verified"),
        ])
        db.commit()
    yield sessions, cipher
    engine.dispose()


def inventory():
    return {
        "yoast_active": True,
        "yoast_version": "26.0",
        "site_name": "Kosmos Test",
        "language": "de-DE",
        "pages": [
            {"id": 10, "page_title": "Startseite", "slug": "start", "url": "https://seo.example/",
             "content": "Webdesign fuer Unternehmen in Bayern.", "seo_title": "Alt", "seo_description": "Alte Beschreibung",
             "indexable": True, "is_front_page": True, "revision": "a" * 64},
            {"id": 11, "page_title": "Impressum", "slug": "impressum", "url": "https://seo.example/impressum/",
             "content": "Anbieterkennzeichnung.", "seo_title": "", "seo_description": "",
             "indexable": False, "is_front_page": False, "revision": "b" * 64},
        ],
    }


def suggestions():
    return {
        10: {"title": "Webdesign fuer Unternehmen in Bayern | Kosmos Test",
             "description": "Kosmos Test entwickelt klare Websites fuer Unternehmen in Bayern, passend zu Marke, Inhalt und Zielgruppe des Betriebs."},
        11: {"title": "Impressum | Kosmos Test",
             "description": "Anbieterkennzeichnung und rechtliche Kontaktdaten von Kosmos Test im vollstaendigen Impressum der Unternehmenswebsite."},
    }


def test_analysis_is_review_only_and_defaults_noindex_to_unselected(context, monkeypatch):
    sessions, cipher = context
    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", lambda *_args, **_kwargs: {"result": inventory()})
    monkeypatch.setattr(SiteSeoService, "_generate_suggestions", lambda *_args, **_kwargs: suggestions())
    with sessions() as db:
        preview = SiteSeoService(db=db, cipher=cipher).analyze(site_id=1, actor="admin")
        assert [row["selected"] for row in preview["rows"]] == [True, False]
        assert "noindex" in " ".join(preview["rows"][1]["warnings"])
        proof = json.loads(cipher.decrypt(preview["preview_token"]))
        assert proof["site_id"] == 1 and set(proof["pages"]) == {"10", "11"}
        assert "content" not in proof["pages"]["10"]


def test_apply_uses_preview_revision_and_returns_rollback(context, monkeypatch):
    sessions, cipher = context
    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", lambda *_args, **_kwargs: {"result": inventory()})
    monkeypatch.setattr(SiteSeoService, "_generate_suggestions", lambda *_args, **_kwargs: suggestions())
    calls = []
    with sessions() as db:
        domain = SiteSeoService(db=db, cipher=cipher)
        preview = domain.analyze(site_id=1, actor="admin")

        def write(_self, _site_id, ability, payload, **_kwargs):
            calls.append((ability, payload))
            return {"result": {"changed": 1, "pages": [{**payload["pages"][0], "revision": "c" * 64}]}}

        monkeypatch.setattr(SiteMcpProxyService, "execute_ability", write)
        edited = {"10": {"title": suggestions()[10]["title"], "description": suggestions()[10]["description"]}}
        result = domain.apply(site_id=1, preview_token=preview["preview_token"], page_ids=[10],
            edited_values_json=json.dumps(edited), actor="admin")
        assert result.data["status"] == "succeeded" and result.data["rollback_token"]
        assert calls[0][0] == "kosmos-bridge/write-page-seo"
        assert calls[0][1]["pages"][0]["revision"] == "a" * 64
        rollback = json.loads(cipher.decrypt(result.data["rollback_token"]))
        assert rollback["pages"][0]["title"] == "Alt"
        assert rollback["pages"][0]["revision"] == "c" * 64


def test_preview_rejects_tampering_and_missing_selected_values(context):
    sessions, cipher = context
    with sessions() as db:
        service = HubOperationService(db=db, cipher=cipher, actor="admin")
        with pytest.raises(SiteSeoError):
            prepare_apply(service, site_id=1, preview_token="tampered", page_ids=[10], edited_values_json="{}")


def test_ai_response_requires_each_exact_page_once():
    response = {"output": [{"type": "function_call", "name": "return_seo_suggestions", "arguments": json.dumps({
        "pages": [{"page_id": 10, "title": "Titel", "description": "Beschreibung"},
                  {"page_id": 10, "title": "Doppelt", "description": "Beschreibung"}],
    })}]}
    with pytest.raises(SiteSeoError):
        SiteSeoService._suggestions_from_response(response, {10, 11})


def test_inventory_rejects_page_url_outside_registered_domain():
    payload = inventory()
    payload["pages"][0]["url"] = "javascript:alert(1)"
    with pytest.raises(SiteSeoError):
        SiteSeoService._inventory(payload, expected_domain="seo.example")


def test_site_template_exposes_seo_only_on_site_detail():
    from pathlib import Path
    site = Path("app/templates/site_detail.html").read_text(encoding="utf-8")
    customer = Path("app/templates/customer_detail.html").read_text(encoding="utf-8")
    assert 'href="#seo"' in site and 'id="seo"' in site and "Website analysieren" in site
    assert "SEO der Website bearbeiten" not in customer
