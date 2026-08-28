import hashlib
import io
import json
import re
import stat
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import BinaryIO
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import SecretCipher, build_request_signature, calculate_body_sha256, signatures_match
from app.models.maintenance_run import MaintenanceRun, MaintenanceRunStatus
from app.models.plugin_installation_package import PluginInstallationPackage
from app.models.request_nonce import RequestNonce
from app.repositories.site_repository import SiteRepository
from app.schemas.registration import RegistrationHeaders


class PluginPackageError(ValueError):
    """Raised when an installation package is not safe to queue."""


@dataclass(frozen=True)
class InspectedPluginPackage:
    original_filename: str
    plugin_file: str
    plugin_name: str
    plugin_version: str
    sha256: str
    package_bytes: bytes


class PluginInstallationPackageService:
    """Fetch and inspect plugin archives before they ever reach a customer site."""

    MAX_PACKAGE_BYTES = 20 * 1024 * 1024
    MAX_UNCOMPRESSED_BYTES = 80 * 1024 * 1024
    MAX_ARCHIVE_ENTRIES = 2_000
    MAX_HEADER_BYTES = 64 * 1024
    RETENTION = timedelta(days=7)
    WORDPRESS_ORG_API = "https://api.wordpress.org/plugins/info/1.2/?{}"
    _SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,199}")
    _PLUGIN_FILE_RE = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._-]*/)*[A-Za-z0-9][A-Za-z0-9._-]*\.php")
    _HEADER_RE = re.compile(r"^\s*(Plugin Name|Version)\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)

    def __init__(self, *, db: Session):
        self.db = db

    def prepare_uploaded_zip(self, *, filename: str, source: BinaryIO) -> PluginInstallationPackage:
        safe_filename = self._safe_filename(filename)
        if not safe_filename.lower().endswith(".zip"):
            raise PluginPackageError("Upload a WordPress plugin ZIP file.")
        package_bytes = self._read_bounded(source)
        inspected = self.inspect_archive(package_bytes=package_bytes, original_filename=safe_filename)
        return self._store(inspected, source="zip-upload")

    def prepare_wordpress_org_plugin(self, *, slug: str) -> PluginInstallationPackage:
        normalized_slug = slug.strip().lower()
        if self._SLUG_RE.fullmatch(normalized_slug) is None:
            raise PluginPackageError("Enter a valid WordPress.org plugin slug, for example wordpress-seo.")

        try:
            info_url = self.WORDPRESS_ORG_API.format(urlencode({"action": "plugin_information", "request[slug]": normalized_slug}))
            with urlopen(Request(info_url, headers={"Accept": "application/json"}), timeout=30) as response:
                info = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise PluginPackageError("WordPress.org could not provide this plugin package.") from exc

        download_url = info.get("download_link") if isinstance(info, dict) else ""
        parsed = urlparse(download_url if isinstance(download_url, str) else "")
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.lower().endswith("wordpress.org"):
            raise PluginPackageError("WordPress.org returned an invalid plugin package URL.")

        try:
            with urlopen(Request(download_url, headers={"Accept": "application/zip"}), timeout=60) as response:
                package_bytes = self._read_bounded(response)
        except PluginPackageError:
            raise
        except Exception as exc:
            raise PluginPackageError("The WordPress.org plugin ZIP could not be downloaded.") from exc

        inspected = self.inspect_archive(
            package_bytes=package_bytes,
            original_filename=f"{normalized_slug}.zip",
        )
        return self._store(inspected, source="wordpress-org")

    def inspect_archive(self, *, package_bytes: bytes, original_filename: str) -> InspectedPluginPackage:
        if not package_bytes:
            raise PluginPackageError("The plugin ZIP is empty.")
        if len(package_bytes) > self.MAX_PACKAGE_BYTES:
            raise PluginPackageError("The plugin ZIP exceeds the 20 MB package limit.")

        try:
            archive = zipfile.ZipFile(io.BytesIO(package_bytes))
            broken_member = archive.testzip()
            entries = archive.infolist()
        except (OSError, zipfile.BadZipFile) as exc:
            raise PluginPackageError("The uploaded file is not a valid ZIP archive.") from exc

        if broken_member:
            raise PluginPackageError("The plugin ZIP contains a damaged file.")
        if not entries or len(entries) > self.MAX_ARCHIVE_ENTRIES:
            raise PluginPackageError("The plugin ZIP has an invalid number of files.")

        top_levels: set[str] = set()
        total_uncompressed = 0
        plugin_headers: list[tuple[str, dict[str, str]]] = []
        for entry in entries:
            path = entry.filename
            if not self._safe_archive_path(path):
                raise PluginPackageError("The plugin ZIP contains an unsafe file path.")
            if stat.S_IFMT(entry.external_attr >> 16) == stat.S_IFLNK:
                raise PluginPackageError("The plugin ZIP contains symbolic links, which are not accepted.")
            total_uncompressed += entry.file_size
            if total_uncompressed > self.MAX_UNCOMPRESSED_BYTES:
                raise PluginPackageError("The unpacked plugin would exceed the 80 MB safety limit.")
            top_levels.add(path.split("/", 1)[0])
            if entry.is_dir() or not path.lower().endswith(".php"):
                continue
            header = archive.read(entry)[: self.MAX_HEADER_BYTES].decode("utf-8", errors="ignore")
            metadata = {key.lower().replace(" ", "_"): value.strip() for key, value in self._HEADER_RE.findall(header)}
            if metadata.get("plugin_name"):
                plugin_headers.append((path, metadata))

        if len(top_levels) != 1 or not next(iter(top_levels), ""):
            raise PluginPackageError("A plugin ZIP must contain exactly one top-level plugin folder.")
        if len(plugin_headers) != 1:
            raise PluginPackageError("The plugin ZIP must contain exactly one WordPress plugin header.")

        plugin_file, metadata = plugin_headers[0]
        plugin_name = metadata.get("plugin_name", "").strip()
        plugin_version = metadata.get("version", "").strip()
        if not plugin_version:
            raise PluginPackageError("The plugin ZIP does not declare a WordPress plugin version.")
        if not self._PLUGIN_FILE_RE.fullmatch(plugin_file) or len(plugin_name) > 255 or len(plugin_version) > 128:
            raise PluginPackageError("The plugin ZIP contains invalid WordPress plugin metadata.")

        return InspectedPluginPackage(
            original_filename=self._safe_filename(original_filename),
            plugin_file=plugin_file,
            plugin_name=plugin_name,
            plugin_version=plugin_version,
            sha256=hashlib.sha256(package_bytes).hexdigest(),
            package_bytes=package_bytes,
        )

    def _store(self, package: InspectedPluginPackage, *, source: str) -> PluginInstallationPackage:
        stored = PluginInstallationPackage(
            source=source,
            original_filename=package.original_filename,
            plugin_file=package.plugin_file,
            plugin_name=package.plugin_name,
            plugin_version=package.plugin_version,
            sha256=package.sha256,
            package_bytes=package.package_bytes,
            expires_at=datetime.now(UTC) + self.RETENTION,
        )
        self.db.add(stored)
        self.db.flush()
        return stored

    def get_for_download(self, package_id: int) -> PluginInstallationPackage | None:
        return self.db.get(PluginInstallationPackage, package_id)

    def authorize_site_download(
        self,
        *,
        request: Request,
        package_id: int,
        cipher: SecretCipher,
    ) -> PluginInstallationPackage:
        """Permit an archive download only to a site in an active install run."""
        headers = RegistrationHeaders.from_request(request.headers)
        if not all([headers.site_uuid, headers.timestamp, headers.nonce, headers.body_sha256, headers.signature]):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Signed Kosmos headers are required.")
        empty_hash = calculate_body_sha256(b"")
        if headers.body_sha256 != empty_hash:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Body hash mismatch.")
        try:
            request_time = datetime.fromisoformat(headers.timestamp)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid request timestamp.") from exc
        if request_time.tzinfo is None:
            request_time = request_time.replace(tzinfo=UTC)
        if abs(datetime.now(UTC) - request_time) > timedelta(minutes=10):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Timestamp outside allowed window.")

        site = SiteRepository(self.db).get_by_uuid(headers.site_uuid)
        if site is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown Kosmos site.")
        connection = next((item for item in site.connections if item.provider == "kosmos-wordpress"), None)
        if connection is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Kosmos site connection is unavailable.")
        expected_signature = build_request_signature(
            site_uuid=headers.site_uuid,
            timestamp=headers.timestamp,
            nonce=headers.nonce,
            body_sha256=headers.body_sha256,
            secret=cipher.decrypt(connection.encrypted_credentials),
        )
        if not signatures_match(expected_signature, headers.signature):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature.")

        self._record_nonce(site_uuid=headers.site_uuid, timestamp=headers.timestamp, nonce=headers.nonce, request_time=request_time)
        package = self.db.get(PluginInstallationPackage, package_id)
        if package is None or (package.expires_at is not None and package.expires_at < datetime.now(UTC)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plugin package is not available.")
        active_run = self.db.scalar(
            select(MaintenanceRun.id).where(
                MaintenanceRun.site_id == site.id,
                MaintenanceRun.kind == "plugin-installation",
                MaintenanceRun.plugin_installation_package_id == package.id,
                MaintenanceRun.status == MaintenanceRunStatus.running.value,
            )
        )
        if active_run is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Plugin package is not queued for this site.")
        return package

    def _record_nonce(self, *, site_uuid: str, timestamp: str, nonce: str, request_time: datetime) -> None:
        now = datetime.now(UTC)
        self.db.execute(delete(RequestNonce).where(RequestNonce.expires_at < now))
        nonce_key = hashlib.sha256(f"{site_uuid}.{timestamp}.{nonce}".encode("utf-8")).hexdigest()
        existing = self.db.scalar(select(RequestNonce.id).where(RequestNonce.key_hash == nonce_key))
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Replay detected.")
        self.db.add(
            RequestNonce(
                key_hash=nonce_key,
                site_uuid=site_uuid,
                expires_at=request_time + timedelta(minutes=10),
            )
        )
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Replay detected.") from exc

    def _read_bounded(self, source: BinaryIO) -> bytes:
        content_length = getattr(source, "headers", {}).get("Content-Length") if hasattr(source, "headers") else None
        if content_length and content_length.isdigit() and int(content_length) > self.MAX_PACKAGE_BYTES:
            raise PluginPackageError("The plugin ZIP exceeds the 20 MB package limit.")
        package_bytes = source.read(self.MAX_PACKAGE_BYTES + 1)
        if len(package_bytes) > self.MAX_PACKAGE_BYTES:
            raise PluginPackageError("The plugin ZIP exceeds the 20 MB package limit.")
        return package_bytes

    @staticmethod
    def _safe_archive_path(path: str) -> bool:
        return bool(path) and "\\" not in path and not path.startswith("/") and ".." not in path.split("/")

    @staticmethod
    def _safe_filename(filename: str) -> str:
        safe = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
        if not safe or len(safe) > 255:
            raise PluginPackageError("The plugin ZIP filename is invalid.")
        return safe
