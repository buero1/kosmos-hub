from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.routes import web
from app.db.base import Base
from app.models.site import Site, SiteStatus
from app.services.customer_communications import CustomerCommunicationAttachmentDownload, CustomerCommunicationCachedImage
from app.services.site_users import SiteUserService


def _site() -> Site:
    return Site(
        uuid="dddb34cd-56ef-78ab-90cd-12ef34ab56cd",
        domain="action-users.example",
        home_url="https://action-users.example/",
        site_url="https://action-users.example/",
        status=SiteStatus.verified.value,
    )


def test_browser_orchestrated_user_actions_call_the_existing_verified_service_methods(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(web, "require_csrf", lambda request, token: None)
    monkeypatch.setattr(web, "_require_hub_admin", lambda request: SimpleNamespace(username="kosmosadmin"))

    def create_user(self, **kwargs):
        calls.append(("create", kwargs))
        return {"username": kwargs["username"]}

    def update_role(self, **kwargs):
        calls.append(("role", kwargs))
        return {"username": "editor"}

    def update_password(self, **kwargs):
        calls.append(("password", kwargs))
        return {"username": "editor"}

    monkeypatch.setattr(SiteUserService, "create_user", create_user)
    monkeypatch.setattr(SiteUserService, "update_role", update_role)
    monkeypatch.setattr(SiteUserService, "update_password", update_password)

    with Session(engine) as db:
        site = _site()
        db.add(site)
        db.commit()
        request = SimpleNamespace()

        create_outcome = web.create_user_on_one_site(
            request=request,
            db=db,
            site_id=site.id,
            username="new-editor",
            email="new-editor@example.test",
            password="A-secure-password-123",
            role="editor",
            csrf_token="csrf",
        )
        role_outcome = web.update_one_user_role(
            request=request,
            db=db,
            site_id=site.id,
            user_id=7,
            role="administrator",
            csrf_token="csrf",
        )
        password_outcome = web.update_one_user_password(
            request=request,
            db=db,
            site_id=site.id,
            user_id=7,
            password="A-secure-password-123",
            csrf_token="csrf",
        )

    assert [outcome["status"] for outcome in (create_outcome, role_outcome, password_outcome)] == ["succeeded", "succeeded", "succeeded"]
    assert [name for name, _ in calls] == ["create", "role", "password"]
    assert calls[0][1]["actor"] == "kosmosadmin"
    assert calls[1][1]["role"] == "administrator"
    assert calls[2][1]["user_id"] == 7


def test_customer_email_attachment_download_returns_a_non_cached_browser_download(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(web, "_require_hub_admin", lambda _request: SimpleNamespace(username="kosmosadmin"))
    monkeypatch.setattr(web, "write_audit_log", lambda _db, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(
        web,
        "_customer_communication_service",
        lambda _db: SimpleNamespace(
            download_email_attachment=lambda **kwargs: (
                calls.append(kwargs)
                or CustomerCommunicationAttachmentDownload(
                    content=b"%PDF-test",
                    content_type="application/pdf",
                    filename="Angebot 2026.pdf",
                )
            )
        ),
    )
    db = SimpleNamespace(commit=lambda: calls.append({"committed": True}), rollback=lambda: None)

    response = web.download_customer_communication_attachment(
        customer_id=73,
        email_id=9,
        attachment_id="zoho-attachment-1",
        request=SimpleNamespace(),
        db=db,
    )

    assert response.body == b"%PDF-test"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == "attachment; filename*=UTF-8''Angebot%202026.pdf"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert calls[0] == {"customer_id": 73, "email_id": 9, "attachment_id": "zoho-attachment-1"}


def test_customer_email_preview_image_returns_the_cached_private_image(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(web, "_require_hub_admin", lambda _request: SimpleNamespace(username="kosmosadmin"))
    monkeypatch.setattr(
        web,
        "_customer_communication_service",
        lambda _db: SimpleNamespace(
            get_email_preview_image=lambda **kwargs: (
                calls.append(kwargs)
                or CustomerCommunicationCachedImage(content=b"image-bytes", content_type="image/png")
            )
        ),
    )
    db = SimpleNamespace(commit=lambda: calls.append({"committed": True}), rollback=lambda: None)

    response = web.display_customer_communication_email_image(
        customer_id=73,
        email_id=9,
        source_url_hash="a" * 64,
        request=SimpleNamespace(),
        db=db,
    )

    assert response.body == b"image-bytes"
    assert response.media_type == "image/png"
    assert response.headers["cache-control"] == "private, max-age=2592000"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert calls[0] == {"customer_id": 73, "email_id": 9, "source_url_hash": "a" * 64}
