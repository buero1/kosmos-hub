"""Encrypted on-disk storage for Hub-generated Finance PDFs."""

from app.core.config import get_settings
from app.core.security import SecretCipher
from app.services.email_attachment_storage import EmailAttachmentStorage, EmailAttachmentStorageError


class FinanceGeneratedPdfStorageError(Exception):
    """Raised when a generated PDF cannot be safely stored or read."""


class FinanceGeneratedPdfStorage:
    def __init__(self, *, cipher: SecretCipher) -> None:
        settings = get_settings()
        self._storage = EmailAttachmentStorage(
            root=settings.finance_generated_pdf_storage_dir,
            cipher=cipher,
            min_free_bytes=settings.finance_invoice_pdf_import_min_free_bytes,
        )

    def store(self, content: bytes) -> str:
        try:
            return self._storage.store(content)
        except EmailAttachmentStorageError as exc:
            raise FinanceGeneratedPdfStorageError(self._message(exc)) from exc

    def load(self, storage_key: str) -> bytes:
        try:
            return self._storage.load(storage_key)
        except EmailAttachmentStorageError as exc:
            raise FinanceGeneratedPdfStorageError(self._message(exc)) from exc

    def remove(self, storage_key: str) -> None:
        self._storage.remove(storage_key)

    @staticmethod
    def _message(error: Exception) -> str:
        return str(error).replace("Anhang", "PDF").replace("anhang", "pdf")
