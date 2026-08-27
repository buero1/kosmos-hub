import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.site_user_snapshot import SiteUserSnapshot
from app.repositories.site_repository import SiteRepository
from app.services.audit import write_audit_log
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService


@dataclass(frozen=True)
class SiteUserInventory:
    snapshot: SiteUserSnapshot
    users: list[dict[str, Any]]


class SiteUserService:
    """Reads and changes WordPress users without retaining credentials in the Hub."""

    LIST_ABILITY = "kosmos-bridge/list-wp-users"
    CREATE_ABILITY = "kosmos-bridge/create-wp-user"
    PASSWORD_ABILITY = "kosmos-bridge/update-wp-user-password"
    DELETE_ABILITY = "kosmos-bridge/delete-wp-user"
    MIN_PASSWORD_LENGTH = 12

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher
        self.repository = SiteRepository(db)
        self.proxy = SiteMcpProxyService(db=db, cipher=cipher)

    def get_latest_inventory(self, site_id: int) -> SiteUserInventory | None:
        snapshot = self.repository.get_latest_site_user_snapshot(site_id)
        if snapshot is None:
            return None
        return SiteUserInventory(snapshot=snapshot, users=self._decrypt_users(snapshot.encrypted_users_json))

    def refresh_site_users(self, site_id: int, *, actor: str = "kosmos-hub") -> SiteUserInventory:
        site = self._site_or_raise(site_id)
        try:
            payload = self.proxy.execute_ability(site_id, self.LIST_ABILITY, {}, timeout_seconds=30)
        except SiteMcpProxyError as exc:
            if exc.code != "KOSMOS_BRIDGE_ABILITY_NOT_FOUND":
                raise
            return self._store_inventory(
                site_id,
                available=False,
                users=[],
                message="This Bridge version does not yet provide WordPress user inventory. Update to Kosmos Bridge 0.3.53 or newer.",
                actor=actor,
            )

        result = payload.get("result", {})
        result = result if isinstance(result, dict) else {}
        return self._store_inventory(
            site_id,
            available=bool(result.get("available", True)),
            users=self._normalize_users(result.get("users")),
            message=self._string_or_none(result.get("message")),
            actor=actor,
        )

    def create_user(
        self,
        *,
        site_id: int,
        username: str,
        email: str,
        password: str,
        role: str,
        display_name: str,
        actor: str,
    ) -> dict[str, Any]:
        normalized_password = self._validated_password(password)
        payload = self.proxy.execute_ability(
            site_id,
            self.CREATE_ABILITY,
            {
                "username": self._required_text(username, "Username"),
                "email": self._required_text(email, "Email"),
                "password": normalized_password,
                "role": self._required_text(role, "Role"),
                "display_name": display_name.strip(),
            },
            timeout_seconds=30,
        )
        user = self._result_user(payload)
        self._write_mutation_audit(site_id, actor, "create-wp-user", f"Created WordPress user {user['username']} (ID {user['id']}).")
        self.refresh_site_users(site_id, actor=actor)
        return user

    def update_password(self, *, site_id: int, user_id: int, password: str, actor: str) -> dict[str, Any]:
        payload = self.proxy.execute_ability(
            site_id,
            self.PASSWORD_ABILITY,
            {"user_id": self._positive_id(user_id, "User"), "password": self._validated_password(password)},
            timeout_seconds=30,
        )
        user = self._result_user(payload)
        self._write_mutation_audit(site_id, actor, "update-wp-user-password", f"Changed the password for WordPress user {user['username']} (ID {user['id']}).")
        self.refresh_site_users(site_id, actor=actor)
        return user

    def delete_user(
        self,
        *,
        site_id: int,
        user_id: int,
        reassign_to_user_id: int,
        confirmed_username: str,
        actor: str,
    ) -> dict[str, Any]:
        inventory = self.get_latest_inventory(site_id)
        target = next((item for item in (inventory.users if inventory else []) if item.get("id") == user_id), None)
        if target is None:
            raise ValueError("Refresh the user inventory before deleting this WordPress user.")
        if confirmed_username.strip() != target["username"]:
            raise ValueError("Enter the exact username of the user to delete as confirmation.")
        target_id = self._positive_id(user_id, "User")
        reassign_id = self._positive_id(reassign_to_user_id, "Replacement user")
        if target_id == reassign_id:
            raise ValueError("Choose a different replacement user for the content reassignment.")

        payload = self.proxy.execute_ability(
            site_id,
            self.DELETE_ABILITY,
            {"user_id": target_id, "reassign_to_user_id": reassign_id},
            timeout_seconds=45,
        )
        result = payload.get("result", {})
        result = result if isinstance(result, dict) else {}
        if result.get("deleted") is not True:
            raise SiteMcpProxyError("WP_USER_DELETE_UNVERIFIED", "WordPress did not confirm that the user was deleted.", status_code=502)
        self._write_mutation_audit(
            site_id,
            actor,
            "delete-wp-user",
            f"Deleted WordPress user {target['username']} (ID {target_id}) and reassigned content to user ID {reassign_id}.",
        )
        self.refresh_site_users(site_id, actor=actor)
        return result

    def _store_inventory(
        self,
        site_id: int,
        *,
        available: bool,
        users: list[dict[str, Any]],
        message: str | None,
        actor: str,
    ) -> SiteUserInventory:
        site = self._site_or_raise(site_id)
        captured_at = datetime.now(UTC)
        snapshot = self.repository.create_site_user_snapshot(
            site=site,
            captured_at=captured_at,
            available=available,
            user_count=len(users),
            encrypted_users_json=self.cipher.encrypt(json.dumps(users, ensure_ascii=False)),
            message=message,
        )
        write_audit_log(
            self.db,
            site=site,
            actor=actor,
            source="hub",
            action="refresh-site-users",
            result="ok" if available else "unsupported",
            detail=f"Stored WordPress user inventory for {site.domain}: {len(users)} user(s).",
        )
        self.db.commit()
        return SiteUserInventory(snapshot=snapshot, users=users)

    def _site_or_raise(self, site_id: int):
        site = self.repository.get_site(site_id)
        if site is None:
            raise SiteMcpProxyError("SITE_NOT_FOUND", f"Site {site_id} was not found.", status_code=404)
        return site

    def _write_mutation_audit(self, site_id: int, actor: str, action: str, detail: str) -> None:
        site = self._site_or_raise(site_id)
        write_audit_log(self.db, site=site, actor=actor, source="hub-web", action=action, result="ok", detail=detail)
        self.db.commit()

    @staticmethod
    def _normalize_users(value: object) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        users: list[dict[str, Any]] = []
        for raw in value:
            if not isinstance(raw, dict):
                continue
            try:
                user_id = int(raw.get("id"))
            except (TypeError, ValueError):
                continue
            username = raw.get("username")
            email = raw.get("email")
            if user_id <= 0 or not isinstance(username, str) or not username.strip() or not isinstance(email, str):
                continue
            roles = raw.get("roles")
            users.append(
                {
                    "id": user_id,
                    "username": username.strip(),
                    "display_name": raw.get("display_name", "").strip() if isinstance(raw.get("display_name"), str) else "",
                    "email": email.strip(),
                    "roles": sorted(role.strip() for role in roles if isinstance(role, str) and role.strip()) if isinstance(roles, list) else [],
                    "registered_at": raw.get("registered_at", "").strip() if isinstance(raw.get("registered_at"), str) else "",
                }
            )
        return sorted(users, key=lambda item: (item["username"].lower(), item["id"]))

    def _decrypt_users(self, encrypted_users_json: str) -> list[dict[str, Any]]:
        try:
            return self._normalize_users(json.loads(self.cipher.decrypt(encrypted_users_json)))
        except Exception:
            return []

    @staticmethod
    def _string_or_none(value: object) -> str | None:
        return value.strip()[:512] if isinstance(value, str) and value.strip() else None

    def _result_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        users = self._normalize_users([result.get("user")]) if isinstance(result, dict) else []
        if not users:
            raise SiteMcpProxyError("WP_USER_RESULT_INVALID", "WordPress did not return the changed user record.", status_code=502)
        return users[0]

    def _validated_password(self, password: str) -> str:
        if len(password) < self.MIN_PASSWORD_LENGTH:
            raise ValueError(f"The password must contain at least {self.MIN_PASSWORD_LENGTH} characters.")
        return password

    @staticmethod
    def _required_text(value: str, label: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{label} is required.")
        return normalized

    @staticmethod
    def _positive_id(value: int, label: str) -> int:
        if value <= 0:
            raise ValueError(f"{label} must be a valid WordPress user.")
        return value
