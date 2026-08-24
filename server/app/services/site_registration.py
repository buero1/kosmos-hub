import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import SecretCipher, build_request_signature, calculate_body_sha256, signatures_match
from app.models.request_nonce import RequestNonce
from app.models.site import Site, SiteStatus
from app.repositories.site_repository import SiteRepository
from app.schemas.registration import RegistrationHeaders, RegistrationRequest, RegistrationResponse
from app.services.audit import write_audit_log


class SiteRegistrationService:
    def __init__(self, *, db: Session, settings: Settings, cipher: SecretCipher):
        self.db = db
        self.settings = settings
        self.cipher = cipher
        self.repository = SiteRepository(db)

    def register(
        self,
        *,
        payload: RegistrationRequest,
        headers: RegistrationHeaders,
        raw_body: bytes,
    ) -> RegistrationResponse:
        site = self.repository.get_by_uuid(payload.site_uuid)
        self._validate_headers(site=site, payload=payload, headers=headers, raw_body=raw_body)

        created = site is None
        if site is None:
            site = Site(
                uuid=payload.site_uuid,
                domain=self._extract_domain(str(payload.home_url)),
                registered_at=payload.registration_timestamp,
            )
            self.db.add(site)

        site.domain = self._extract_domain(str(payload.home_url))
        site.home_url = str(payload.home_url)
        site.site_url = str(payload.site_url)
        site.wordpress_version = payload.wordpress_version
        site.php_version = payload.php_version
        site.bridge_version = payload.bridge_version
        site.last_seen_at = payload.registration_timestamp
        if site.registered_at is None:
            site.registered_at = payload.registration_timestamp

        if self._is_auto_verified(site.domain):
            site.status = SiteStatus.verified.value
            if site.verified_at is None:
                site.verified_at = payload.registration_timestamp
        elif site.status in {None, "", SiteStatus.unknown.value}:  # type: ignore[arg-type]
            site.status = SiteStatus.pending.value

        connection = self.repository.get_or_create_connection(site, "kosmos-wordpress")
        connection.endpoint = str(payload.mcp_endpoint or payload.site_url)
        connection.auth_type = "hmac-sha256"
        connection.status = "active"
        if created:
            if not payload.site_secret:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Bootstrap registration requires a site secret.",
                )
            connection.encrypted_credentials = self.cipher.encrypt(payload.site_secret)
        elif payload.site_secret:
            current_secret = self.cipher.decrypt(connection.encrypted_credentials)
            if current_secret != payload.site_secret:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Stored site secret mismatch. Re-onboarding is required before rotating credentials.",
                )
        connection.last_success_at = payload.registration_timestamp

        write_audit_log(
            self.db,
            site=site,
            actor="kosmos-bridge",
            source="wordpress",
            action="heartbeat" if payload.heartbeat else "registration",
            result="ok",
            detail=f"Site {site.domain} reported {payload.wordpress_version} / PHP {payload.php_version}.",
            request_id=headers.request_id,
        )

        self.db.commit()
        self.db.refresh(site)

        message = "Site created." if created else "Site updated."
        return RegistrationResponse(site_id=site.id, site_uuid=site.uuid, status=site.status, message=message)

    def _validate_headers(
        self,
        *,
        site: Site | None,
        payload: RegistrationRequest,
        headers: RegistrationHeaders,
        raw_body: bytes,
    ) -> None:
        if not all([headers.site_uuid, headers.timestamp, headers.nonce, headers.body_sha256, headers.signature]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Signed Kosmos headers are required.",
            )

        if headers.site_uuid and headers.site_uuid != payload.site_uuid:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Header site UUID mismatch.")

        if headers.body_sha256:
            actual_hash = calculate_body_sha256(raw_body)
            if actual_hash != headers.body_sha256:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Body hash mismatch.")

        request_time = datetime.fromisoformat(headers.timestamp)
        if request_time.tzinfo is None:
            request_time = request_time.replace(tzinfo=UTC)
        if abs(datetime.now(UTC) - request_time) > timedelta(minutes=10):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Timestamp outside allowed window.")

        if site is None:
            if not payload.site_secret:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Bootstrap registration requires a site secret.",
                )
            expected_signature = build_request_signature(
                site_uuid=headers.site_uuid,
                timestamp=headers.timestamp,
                nonce=headers.nonce,
                body_sha256=headers.body_sha256,
                secret=payload.site_secret,
            )
            if not signatures_match(expected_signature, headers.signature):
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bootstrap signature.")
            self._record_nonce(site_uuid=payload.site_uuid, timestamp=headers.timestamp, nonce=headers.nonce, request_time=request_time)
            return

        connection = self.repository.get_or_create_connection(site, "kosmos-wordpress")
        secret = self.cipher.decrypt(connection.encrypted_credentials)
        expected_signature = build_request_signature(
            site_uuid=headers.site_uuid,
            timestamp=headers.timestamp,
            nonce=headers.nonce,
            body_sha256=headers.body_sha256,
            secret=secret,
        )
        if not signatures_match(expected_signature, headers.signature):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature.")

        self._record_nonce(site_uuid=payload.site_uuid, timestamp=headers.timestamp, nonce=headers.nonce, request_time=request_time)

    def _record_nonce(self, *, site_uuid: str, timestamp: str, nonce: str, request_time: datetime) -> None:
        now = datetime.now(UTC)
        self.db.execute(delete(RequestNonce).where(RequestNonce.expires_at < now))

        nonce_key = hashlib.sha256(f"{site_uuid}.{timestamp}.{nonce}".encode("utf-8")).hexdigest()
        exists = self.db.scalar(
            select(RequestNonce.id).where(
                RequestNonce.key_hash == nonce_key,
                RequestNonce.expires_at >= now,
            )
        )
        if exists is not None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Replay detected.")

        self.db.add(
            RequestNonce(
                key_hash=nonce_key,
                site_uuid=site_uuid,
                expires_at=request_time + timedelta(minutes=10),
            )
        )
        try:
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Replay detected.") from exc

    def _extract_domain(self, url: str) -> str:
        return (urlparse(url).hostname or "").lower()

    def _is_auto_verified(self, domain: str) -> bool:
        domain = domain.lower()
        for allowed in self.settings.auto_verify_domain_list:
            if domain == allowed or domain.endswith(f".{allowed}"):
                return True
        return False
