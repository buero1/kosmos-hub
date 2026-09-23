"""Encrypted on-disk storage for email attachments outside the relational database."""

from __future__ import annotations

from os import fsync, replace
from pathlib import Path
from secrets import token_urlsafe
from shutil import disk_usage
from tempfile import NamedTemporaryFile

from cryptography.fernet import InvalidToken
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.security import SecretCipher


class EmailAttachmentStorageError(Exception):
    """Raised when an attachment cannot be safely written or read."""


def track_attachment_file(db: Session, storage: "EmailAttachmentStorage", key: str, *, removed: bool = False) -> None:
    """Keep filesystem changes consistent with commits, rollbacks and savepoints."""
    if not db.info.get("attachment_file_hooks"):
        db.info["attachment_file_hooks"] = True

        def committed(session):
            transaction = session.get_nested_transaction() or session.get_transaction()
            pending = session.info.get("attachment_files", [])
            if transaction is not None and transaction.nested:
                session.info["attachment_files"] = [
                    (transaction.parent if tx is transaction else tx, store, file_key, deleted)
                    for tx, store, file_key, deleted in pending
                ]
            else:
                for _tx, store, file_key, deleted in pending:
                    if deleted:
                        store.remove(file_key)
                session.info["attachment_files"] = []

        def ended(session, transaction):
            # Successful commits consumed/reparented their entries above. Anything
            # left at transaction end was rolled back (including Session.close()).
            pending = session.info.get("attachment_files", [])
            for tx, store, file_key, deleted in pending:
                if tx is transaction and not deleted:
                    store.remove(file_key)
            session.info["attachment_files"] = [item for item in pending if item[0] is not transaction]

        event.listen(db, "after_commit", committed)
        event.listen(db, "after_transaction_end", ended)
    transaction = db.get_nested_transaction() or db.get_transaction()
    db.info.setdefault("attachment_files", []).append((transaction, storage, key, removed))


class EmailAttachmentStorage:
    """Stores opaque, encrypted blobs using server-generated keys only."""

    def __init__(self, *, root: str | Path, cipher: SecretCipher, min_free_bytes: int) -> None:
        self.root = Path(root).expanduser().resolve()
        self.cipher = cipher
        self.min_free_bytes = min_free_bytes

    def ensure_ready(self) -> None:
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.root.chmod(0o700)
        except OSError as exc:
            raise EmailAttachmentStorageError("Der sichere Anhangspeicher konnte nicht vorbereitet werden.") from exc
        if disk_usage(self.root).free < self.min_free_bytes:
            raise EmailAttachmentStorageError("Für den Anhangsimport ist nicht genug freier Speicherplatz verfügbar.")

    def store(self, content: bytes) -> str:
        if not content:
            raise EmailAttachmentStorageError("Ein leerer Anhang wird nicht gespeichert.")
        self.ensure_ready()
        encrypted_content = self.cipher.encrypt_bytes(content)
        if disk_usage(self.root).free - len(encrypted_content) < self.min_free_bytes:
            raise EmailAttachmentStorageError("Für diesen Anhang ist nicht genug freier Speicherplatz verfügbar.")

        storage_key = token_urlsafe(32)
        destination = self._path(storage_key)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with NamedTemporaryFile(mode="wb", dir=destination.parent, prefix=".upload-", delete=False) as temporary:
                temporary.write(encrypted_content)
                temporary.flush()
                fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            temporary_path.chmod(0o600)
            replace(temporary_path, destination)
        except OSError as exc:
            try:
                temporary_path.unlink(missing_ok=True)
            except UnboundLocalError:
                pass
            raise EmailAttachmentStorageError("Der Anhang konnte nicht sicher gespeichert werden.") from exc
        return storage_key

    def load(self, storage_key: str) -> bytes:
        path = self._path(storage_key)
        try:
            encrypted_content = path.read_bytes()
            return self.cipher.decrypt_bytes(encrypted_content)
        except (InvalidToken, OSError, ValueError) as exc:
            raise EmailAttachmentStorageError("Der gespeicherte Anhang ist nicht lesbar.") from exc

    def remove(self, storage_key: str) -> None:
        try:
            self._path(storage_key).unlink(missing_ok=True)
        except OSError:
            pass

    def _path(self, storage_key: str) -> Path:
        if not storage_key or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in storage_key):
            raise EmailAttachmentStorageError("Der Speicherverweis für den Anhang ist ungültig.")
        return self.root / storage_key[:2] / f"{storage_key}.bin"
