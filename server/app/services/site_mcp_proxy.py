import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib import error, request

from sqlalchemy.orm import Session

from app.core.security import SecretCipher, build_request_signature, calculate_body_sha256
from app.models.site import Site
from app.models.site_connection import SiteConnection
from app.repositories.site_repository import SiteRepository
from app.services.audit import write_audit_log


class SiteMcpProxyError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 500):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass
class RemoteResult:
    payload: dict[str, Any]
    request_id: str


class SiteMcpProxyService:
    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher
        self.repository = SiteRepository(db)

    def discover_abilities(self, site_id: int) -> dict[str, Any]:
        site, connection = self._get_site_and_connection(site_id)
        result = self._send(site, connection, "discover-abilities", {})
        self._record_success(site, "discover-abilities", f"Discovered abilities for {site.domain}.", result.request_id)
        return result.payload

    def get_ability_info(self, site_id: int, ability_name: str) -> dict[str, Any]:
        site, connection = self._get_site_and_connection(site_id)
        result = self._send(site, connection, "get-ability-info", {"ability_name": ability_name})
        self._record_success(site, "get-ability-info", f"Fetched ability info for {ability_name}.", result.request_id)
        return result.payload

    def execute_ability(self, site_id: int, ability_name: str, ability_input: dict[str, Any] | None) -> dict[str, Any]:
        site, connection = self._get_site_and_connection(site_id)
        result = self._send(
            site,
            connection,
            "execute-ability",
            {"ability_name": ability_name, "input": ability_input},
        )
        self._record_success(site, "execute-ability", f"Executed {ability_name}.", result.request_id)
        return result.payload

    def _get_site_and_connection(self, site_id: int) -> tuple[Site, SiteConnection]:
        site = self.repository.get_site(site_id)
        if site is None:
            raise SiteMcpProxyError("SITE_NOT_FOUND", f"Site {site_id} was not found.", status_code=404)

        for connection in site.connections:
            if connection.provider == "kosmos-wordpress" and connection.endpoint:
                return site, connection

        raise SiteMcpProxyError(
            "MCP_NOT_AVAILABLE",
            f"Site {site.domain} has no kosmos-wordpress connection.",
            status_code=424,
        )

    def _send(self, site: Site, connection: SiteConnection, action: str, payload: dict[str, Any]) -> RemoteResult:
        endpoint = connection.endpoint.rstrip("/")
        url = f"{endpoint}/{action}"
        body = json.dumps(payload).encode("utf-8")
        timestamp = datetime.now(UTC).isoformat()
        nonce = uuid.uuid4().hex[:24]
        body_sha256 = calculate_body_sha256(body)
        secret = self.cipher.decrypt(connection.encrypted_credentials)
        signature = build_request_signature(site.uuid, timestamp, nonce, body_sha256, secret)
        request_id = str(uuid.uuid4())

        req = request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Request-Id": request_id,
                "X-Kosmos-Site-UUID": site.uuid,
                "X-Kosmos-Timestamp": timestamp,
                "X-Kosmos-Nonce": nonce,
                "X-Kosmos-Body-SHA256": body_sha256,
                "X-Kosmos-Signature": signature,
            },
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=20) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            parsed = self._read_error_body(exc)
            self._record_error(site, action, parsed.get("message", str(exc)), request_id)
            raise SiteMcpProxyError(
                parsed.get("code", "REMOTE_HTTP_ERROR").upper(),
                parsed.get("message", str(exc)),
                status_code=exc.code,
            ) from exc
        except error.URLError as exc:
            self._record_error(site, action, str(exc.reason), request_id)
            raise SiteMcpProxyError("REMOTE_TIMEOUT", f"Site request failed: {exc.reason}", status_code=504) from exc

        connection.last_success_at = datetime.now(UTC)
        site.last_seen_at = datetime.now(UTC)
        self.db.commit()
        return RemoteResult(payload=parsed, request_id=request_id)

    def _record_success(self, site: Site, action: str, detail: str, request_id: str) -> None:
        write_audit_log(
            self.db,
            site=site,
            actor="kosmos-hub",
            source="hub",
            action=action,
            result="ok",
            detail=detail,
            request_id=request_id,
        )
        self.db.commit()

    def _record_error(self, site: Site, action: str, detail: str, request_id: str) -> None:
        write_audit_log(
            self.db,
            site=site,
            actor="kosmos-hub",
            source="hub",
            action=action,
            result="error",
            detail=detail,
            request_id=request_id,
        )
        self.db.commit()

    def _read_error_body(self, exc: error.HTTPError) -> dict[str, Any]:
        try:
            data = json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"code": "REMOTE_HTTP_ERROR", "message": str(exc)}

        if isinstance(data, dict) and "message" in data:
            return data

        return {"code": "REMOTE_HTTP_ERROR", "message": json.dumps(data)}

