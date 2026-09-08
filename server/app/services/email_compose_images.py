"""Local image uploads for the email composer and inline delivery preparation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from secrets import token_urlsafe

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import SecretCipher
from app.models.email_compose_image import EmailComposeImage
from app.services.email_attachment_storage import EmailAttachmentStorage, EmailAttachmentStorageError


_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_LOCAL_IMAGE_PATH_PATTERN = re.compile(r"^/emails/compose/images/(?P<token>[A-Za-z0-9_-]{32,64})$")
_LOCAL_IMAGE_SRC_PATTERN = re.compile(
    r"(?P<prefix><img\b[^>]*?\bsrc\s*=\s*)(?P<quote>['\"])(?P<source>/emails/compose/images/(?P<token>[A-Za-z0-9_-]{32,64}))(?P=quote)",
    flags=re.IGNORECASE,
)


class EmailComposeImageError(ValueError):
    """Raised when a local compose image is invalid or unavailable."""


@dataclass(frozen=True)
class EmailComposeInlineImage:
    content_id: str
    filename: str
    content: bytes
    content_type: str


class EmailComposeImageService:
    """Store only verified local images and turn them into inline MIME parts on send."""

    def __init__(self, *, db: Session, cipher: SecretCipher) -> None:
        self.db = db
        settings = get_settings()
        self.storage = EmailAttachmentStorage(
            root=settings.email_attachment_storage_dir,
            cipher=cipher,
            min_free_bytes=settings.email_attachment_import_min_free_bytes,
        )

    def store_upload(
        self,
        *,
        filename: str,
        content: bytes,
        submitted_content_type: str | None,
        user_id: int,
    ) -> EmailComposeImage:
        normalized_filename = re.split(r"[/\\]", filename.strip())[-1].strip()
        if not normalized_filename or len(normalized_filename) > 255:
            raise EmailComposeImageError("Der Bilddateiname ist ungültig.")
        if not content:
            raise EmailComposeImageError("Die Bilddatei ist leer.")
        if len(content) > _MAX_IMAGE_BYTES:
            raise EmailComposeImageError("Ein eingefügtes Bild darf höchstens 5 MB groß sein.")
        content_type = self._detected_content_type(content)
        if submitted_content_type and submitted_content_type.casefold() != content_type:
            raise EmailComposeImageError("Die Bilddatei stimmt nicht mit ihrem Dateityp überein.")

        try:
            storage_key = self.storage.store(content)
        except EmailAttachmentStorageError as exc:
            raise EmailComposeImageError(str(exc)) from exc

        image = EmailComposeImage(
            token=token_urlsafe(24),
            storage_key=storage_key,
            filename=normalized_filename,
            content_type=content_type,
            byte_size=len(content),
            created_by_user_id=user_id,
        )
        try:
            self.db.add(image)
            self.db.flush()
        except Exception:
            self.storage.remove(storage_key)
            raise
        return image

    def load_image(self, *, token: str) -> tuple[EmailComposeImage, bytes]:
        image = self._image_for_token(token)
        try:
            return image, self.storage.load(image.storage_key)
        except EmailAttachmentStorageError as exc:
            raise EmailComposeImageError("Das lokal gespeicherte Bild ist nicht lesbar.") from exc

    def prepare_inline_images(self, content: str) -> tuple[str, tuple[EmailComposeInlineImage, ...]]:
        """Replace only Hub-local image references with content IDs for SMTP delivery."""
        images: dict[str, EmailComposeInlineImage] = {}

        def replace(match: re.Match[str]) -> str:
            token = match.group("token")
            inline = images.get(token)
            if inline is None:
                image, binary = self.load_image(token=token)
                inline = EmailComposeInlineImage(
                    content_id=f"hub-image-{image.id}@kosmos-hub",
                    filename=image.filename,
                    content=binary,
                    content_type=image.content_type,
                )
                images[token] = inline
            return f'{match.group("prefix")}{match.group("quote")}cid:{inline.content_id}{match.group("quote")}'

        return _LOCAL_IMAGE_SRC_PATTERN.sub(replace, content), tuple(images.values())

    @staticmethod
    def is_local_image_path(value: str) -> bool:
        return bool(_LOCAL_IMAGE_PATH_PATTERN.fullmatch(value.strip()))

    def _image_for_token(self, token: str) -> EmailComposeImage:
        if not _LOCAL_IMAGE_PATH_PATTERN.fullmatch(f"/emails/compose/images/{token}"):
            raise EmailComposeImageError("Der lokale Bildverweis ist ungültig.")
        image = self.db.scalar(select(EmailComposeImage).where(EmailComposeImage.token == token))
        if image is None:
            raise EmailComposeImageError("Das eingefügte Bild ist im Hub nicht mehr verfügbar.")
        return image

    @staticmethod
    def _detected_content_type(content: bytes) -> str:
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            return "image/webp"
        raise EmailComposeImageError("Erlaubt sind nur PNG-, JPEG-, GIF- oder WebP-Bilder.")
