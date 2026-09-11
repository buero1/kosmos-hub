"""Dedicated encrypted storage for imported invoice PDF snapshots."""

from app.core.config import get_settings
from app.core.security import SecretCipher
from app.services.email_attachment_storage import EmailAttachmentStorage, EmailAttachmentStorageError


class FinanceInvoicePdfStorageError(Exception):
    """Raised when an invoice PDF cannot be safely stored or read."""


class FinanceInvoicePdfStorage:
    def __init__(self, *, cipher: SecretCipher) -> None:
        settings = get_settings()
        self._storage = EmailAttachmentStorage(
            root=settings.finance_invoice_pdf_storage_dir,
            cipher=cipher,
            min_free_bytes=settings.finance_invoice_pdf_import_min_free_bytes,
        )

    def ensure_ready(self) -> None:
        try:
            self._storage.ensure_ready()
        except EmailAttachmentStorageError as exc:
            raise FinanceInvoicePdfStorageError(self._message(exc)) from exc

    def store(self, content: bytes) -> str:
        try:
            return self._storage.store(content)
        except EmailAttachmentStorageError as exc:
            raise FinanceInvoicePdfStorageError(self._message(exc)) from exc

    def load(self, storage_key: str) -> bytes:
        try:
            return self._storage.load(storage_key)
        except EmailAttachmentStorageError as exc:
            raise FinanceInvoicePdfStorageError(self._message(exc)) from exc

    def remove(self, storage_key: str) -> None:
        self._storage.remove(storage_key)

    @staticmethod
    def _message(error: Exception) -> str:
        return str(error).replace("Anhang", "Rechnungs-PDF").replace("anhang", "rechnungs-pdf")
