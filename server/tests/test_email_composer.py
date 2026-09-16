from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.services.customer_communications import CustomerCommunicationService
from app.services.email_compose_images import EmailComposeImageService
from app.services.email_composer_settings import EmailComposerSettingsError, EmailComposerSettingsService


def test_mail_composer_settings_are_persisted_with_safe_defaults():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = EmailComposerSettingsService(db=db)
        assert service.get_runtime_settings().font_family_key == "verdana"
        assert service.get_runtime_settings().font_size == 12
        assert service.get_runtime_settings().line_height == 1.1

        configured = service.configure(
            font_family_key="georgia",
            font_size=16,
            line_height=1.3,
        )
        db.commit()

        assert configured.font_family == "Georgia, Times New Roman, serif"
        assert configured.font_size == 16
        assert configured.line_height == 1.3
        assert service.apply_default_style("<p>Hallo</p>") == (
            '<div style="font-family: Georgia, Times New Roman, serif; font-size: 16px; line-height: 1.3"><p>Hallo</p></div>'
        )
        with pytest.raises(EmailComposerSettingsError):
            service.configure(
                font_family_key="custom-css",
                font_size=16,
                line_height=1.3,
            )


def test_mail_composer_settings_persist_a_shared_signature():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = EmailComposerSettingsService(db=db)
        signature = '<p>Viele Grüße<br><img src="/emails/compose/images/' + "a" * 32 + '" alt="Kosmos"></p>'

        configured = service.configure_signature(signature_html=signature)
        db.commit()

        assert configured.signature_html == signature
        assert service.configure(
            font_family_key="verdana",
            font_size=12,
            line_height=1.1,
        ).signature_html == signature


def test_mail_composer_allows_only_hub_local_image_paths():
    token = "a" * 32
    content = CustomerCommunicationService._sanitized_email_content(
        f'<img src="/emails/compose/images/{token}" alt="Logo">'
    )

    assert content == f'<img src="/emails/compose/images/{token}" alt="Logo">'
    with pytest.raises(ValueError, match="Nachricht darf nicht leer"):
        CustomerCommunicationService._sanitized_email_content('<img src="file:///private/logo.png">')


def test_compose_images_can_be_embedded_for_offline_pdf_rendering(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    token = "a" * 32
    loads: list[str] = []

    with Session(engine) as db:
        service = EmailComposeImageService(db=db, cipher=SecretCipher("a" * 32))

        def load_image(*, token: str):
            loads.append(token)
            return SimpleNamespace(content_type="image/png"), b"logo-bytes"

        monkeypatch.setattr(service, "load_image", load_image)
        content = (
            f'<img src="/emails/compose/images/{token}" alt="Logo">'
            f'<img src="/emails/compose/images/{token}" alt="Logo erneut">'
            '<img src="https://example.test/external.png" alt="Extern">'
        )

        embedded = service.embed_local_images(content)

    assert embedded.count('src="data:image/png;base64,bG9nby1ieXRlcw=="') == 2
    assert 'src="https://example.test/external.png"' in embedded
    assert loads == [token]
