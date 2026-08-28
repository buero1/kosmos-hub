import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.site import Site
from app.models.site_user_snapshot import SiteUserSnapshot
from app.repositories.site_repository import SiteRepository
from app.services.audit import write_audit_log
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService


@dataclass(frozen=True)
class SiteUserInventory:
    snapshot: SiteUserSnapshot
    users: list[dict[str, Any]]


@dataclass(frozen=True)
class UserWorkbenchEntry:
    """One stored WordPress user, enriched with its managed site context."""

    site: Site
    snapshot: SiteUserSnapshot
    user: dict[str, Any]
    supports_password_change: bool
    supports_role_change: bool
    supports_delete: bool

    @property
    def key(self) -> str:
        return f"{self.site.id}:{self.user['id']}"


class SiteUserService:
    """Reads and changes WordPress users without retaining credentials in the Hub."""

    LIST_ABILITY = "kosmos-bridge/list-wp-users"
    CREATE_ABILITY = "kosmos-bridge/create-wp-user"
    PASSWORD_ABILITY = "kosmos-bridge/update-wp-user-password"
    ROLE_ABILITY = "kosmos-bridge/update-wp-user-role"
    DELETE_ABILITY = "kosmos-bridge/delete-wp-user"
    MIN_PASSWORD_LENGTH = 12
    BULK_ACTION_LIMIT = 10
    ROLE_OPTIONS = ("subscriber", "contributor", "author", "editor", "administrator")

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

    def list_workbench_entries(self) -> list[UserWorkbenchEntry]:
        sites = self.repository.list_sites(limit=1000)
        snapshots = self.repository.get_latest_user_snapshots_by_site_ids([site.id for site in sites])
        entries: list[UserWorkbenchEntry] = []
        for site in sites:
            snapshot = snapshots.get(site.id)
            if snapshot is None or not snapshot.available:
                continue
            ability_names = {capability.ability_name for capability in site.capabilities}
            for wp_user in self._decrypt_users(snapshot.encrypted_users_json):
                entries.append(
                    UserWorkbenchEntry(
                        site=site,
                        snapshot=snapshot,
                        user=wp_user,
                        supports_password_change=self.PASSWORD_ABILITY in ability_names,
                        supports_role_change=self.ROLE_ABILITY in ability_names,
                        supports_delete=self.DELETE_ABILITY in ability_names,
                    )
                )
        return sorted(entries, key=lambda entry: (entry.site.domain.casefold(), entry.user["username"].casefold(), entry.user["id"]))

    @staticmethod
    def filter_workbench_entries(
        entries: list[UserWorkbenchEntry],
        *,
        query: str = "",
        site_id: int | None = None,
        site_ids: set[int] | None = None,
        role: str = "all",
        customer_status: str = "all",
    ) -> list[UserWorkbenchEntry]:
        normalized_query = query.strip().casefold()
        selected_site_ids = site_ids if site_ids is not None else ({site_id} if site_id is not None else None)

        def matches(entry: UserWorkbenchEntry) -> bool:
            if selected_site_ids is not None and entry.site.id not in selected_site_ids:
                return False
            if role != "all" and role not in entry.user["roles"]:
                return False
            customer = entry.site.customer
            if customer_status != "all" and (customer is None or customer.zoho_status != customer_status):
                return False
            if not normalized_query:
                return True
            values = (
                entry.site.domain,
                customer.name if customer is not None else "",
                customer.zoho_status if customer is not None and customer.zoho_status else "",
                entry.user["username"],
                entry.user["display_name"],
                entry.user["email"],
                " ".join(entry.user["roles"]),
            )
            return normalized_query in " ".join(values).casefold()

        return [entry for entry in entries if matches(entry)]

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
        refresh_inventory: bool = True,
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
        if refresh_inventory:
            self.refresh_site_users(site_id, actor=actor)
        return user

    def update_password(
        self,
        *,
        site_id: int,
        user_id: int,
        password: str,
        actor: str,
        refresh_inventory: bool = True,
    ) -> dict[str, Any]:
        payload = self.proxy.execute_ability(
            site_id,
            self.PASSWORD_ABILITY,
            {"user_id": self._positive_id(user_id, "User"), "password": self._validated_password(password)},
            timeout_seconds=30,
        )
        user = self._result_user(payload)
        self._write_mutation_audit(site_id, actor, "update-wp-user-password", f"Changed the password for WordPress user {user['username']} (ID {user['id']}).")
        if refresh_inventory:
            self.refresh_site_users(site_id, actor=actor)
        return user

    def update_role(
        self,
        *,
        site_id: int,
        user_id: int,
        role: str,
        actor: str,
        refresh_inventory: bool = True,
    ) -> dict[str, Any]:
        normalized_role = self._validated_role(role)
        payload = self.proxy.execute_ability(
            site_id,
            self.ROLE_ABILITY,
            {"user_id": self._positive_id(user_id, "User"), "role": normalized_role},
            timeout_seconds=30,
        )
        user = self._result_user(payload)
        if normalized_role not in user["roles"]:
            raise SiteMcpProxyError("WP_USER_ROLE_UNVERIFIED", "WordPress did not return the requested user role.", status_code=502)
        self._write_mutation_audit(site_id, actor, "update-wp-user-role", f"Changed the role for WordPress user {user['username']} (ID {user['id']}) to {normalized_role}.")
        if refresh_inventory:
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
        refresh_inventory: bool = True,
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
        if refresh_inventory:
            self.refresh_site_users(site_id, actor=actor)
        return result

    def update_passwords_bulk(self, *, selected_keys: list[str], password: str, actor: str) -> list[dict[str, str]]:
        normalized_password = self._validated_password(password)
        targets = self._selected_workbench_entries(selected_keys)
        return self._run_bulk_mutation(
            targets,
            lambda target: self.update_password(
                site_id=target.site.id,
                user_id=target.user["id"],
                password=normalized_password,
                actor=actor,
                refresh_inventory=False,
            ),
            actor=actor,
        )

    def create_users_bulk(
        self,
        *,
        site_ids: list[int],
        username: str,
        email: str,
        password: str,
        role: str,
        actor: str,
    ) -> list[dict[str, str]]:
        targets = self._selected_sites(site_ids)
        normalized_username = self._required_text(username, "Username")
        normalized_email = self._required_text(email, "Email")
        normalized_password = self._validated_password(password)
        normalized_role = self._validated_role(role)
        outcomes: list[dict[str, str]] = []
        changed_site_ids: set[int] = set()

        for site in targets:
            try:
                created = self.create_user(
                    site_id=site.id,
                    username=normalized_username,
                    email=normalized_email,
                    password=normalized_password,
                    role=normalized_role,
                    display_name="",
                    actor=actor,
                    refresh_inventory=False,
                )
            except (SiteMcpProxyError, ValueError) as exc:
                outcomes.append(self._bulk_site_outcome(site, normalized_username, "failed", str(exc)))
            else:
                changed_site_ids.add(site.id)
                outcomes.append(
                    self._bulk_site_outcome(
                        site,
                        str(created.get("username") or normalized_username),
                        "succeeded",
                        "Created and verified by WordPress.",
                    )
                )

        self._refresh_changed_site_users(changed_site_ids, outcomes, actor=actor)
        return outcomes

    def update_roles_bulk(self, *, selected_keys: list[str], role: str, actor: str) -> list[dict[str, str]]:
        normalized_role = self._validated_role(role)
        targets = self._selected_workbench_entries(selected_keys)
        return self._run_bulk_mutation(
            targets,
            lambda target: self.update_role(
                site_id=target.site.id,
                user_id=target.user["id"],
                role=normalized_role,
                actor=actor,
                refresh_inventory=False,
            ),
            actor=actor,
        )

    def delete_users_bulk(
        self,
        *,
        selected_keys: list[str],
        reassign_to_user_ids: list[int],
        actor: str,
    ) -> list[dict[str, str]]:
        targets = self._selected_workbench_entries(selected_keys)
        if len(targets) != len(reassign_to_user_ids):
            raise ValueError("Choose a content reassignment user for every selected WordPress user.")
        replacements = dict(zip((target.key for target in targets), reassign_to_user_ids, strict=True))
        return self._run_bulk_mutation(
            targets,
            lambda target: self.delete_user(
                site_id=target.site.id,
                user_id=target.user["id"],
                reassign_to_user_id=replacements[target.key],
                confirmed_username=target.user["username"],
                actor=actor,
                refresh_inventory=False,
            ),
            actor=actor,
        )

    def selected_workbench_entries(self, selected_keys: list[str]) -> list[UserWorkbenchEntry]:
        return self._selected_workbench_entries(selected_keys)

    def _selected_workbench_entries(self, selected_keys: list[str]) -> list[UserWorkbenchEntry]:
        keys = list(dict.fromkeys(key.strip() for key in selected_keys if key and key.strip()))
        if not keys:
            raise ValueError("Select at least one WordPress user.")
        if len(keys) > self.BULK_ACTION_LIMIT:
            raise ValueError(f"Select at most {self.BULK_ACTION_LIMIT} WordPress users per bulk action.")
        entries_by_key = {entry.key: entry for entry in self.list_workbench_entries()}
        missing = [key for key in keys if key not in entries_by_key]
        if missing:
            raise ValueError("One or more selected WordPress users are no longer present in the stored inventory. Refresh the affected site first.")
        return [entries_by_key[key] for key in keys]

    def _selected_sites(self, site_ids: list[int]) -> list:
        unique_ids = list(dict.fromkeys(site_id for site_id in site_ids if site_id > 0))
        if not unique_ids:
            raise ValueError("Select at least one site from the site panel before creating a WordPress user.")
        if len(unique_ids) > self.BULK_ACTION_LIMIT:
            raise ValueError(f"Select at most {self.BULK_ACTION_LIMIT} sites per user creation.")
        sites = []
        for site_id in unique_ids:
            site = self.repository.get_site(site_id)
            if site is None:
                raise ValueError("One or more selected sites are no longer registered in Kosmos Hub.")
            sites.append(site)
        return sites

    def _run_bulk_mutation(self, targets: list[UserWorkbenchEntry], operation, *, actor: str) -> list[dict[str, str]]:
        outcomes: list[dict[str, str]] = []
        changed_site_ids: set[int] = set()
        for target in targets:
            try:
                operation(target)
            except (SiteMcpProxyError, ValueError) as exc:
                outcomes.append(self._bulk_outcome(target, "failed", str(exc)))
            else:
                changed_site_ids.add(target.site.id)
                outcomes.append(self._bulk_outcome(target, "succeeded", "Completed and verified by WordPress."))

        self._refresh_changed_site_users(changed_site_ids, outcomes, actor=actor)
        return outcomes

    def _refresh_changed_site_users(self, changed_site_ids: set[int], outcomes: list[dict[str, str]], *, actor: str) -> None:
        for site_id in changed_site_ids:
            try:
                self.refresh_site_users(site_id, actor=actor)
            except SiteMcpProxyError as exc:
                for outcome in outcomes:
                    if outcome["site_id"] == str(site_id) and outcome["status"] == "succeeded":
                        outcome["message"] = f"Completed, but the stored inventory could not refresh: {exc.message}"

    @staticmethod
    def _bulk_outcome(target: UserWorkbenchEntry, status: str, message: str) -> dict[str, str]:
        return {
            "site_id": str(target.site.id),
            "site": target.site.domain,
            "username": target.user["username"],
            "status": status,
            "message": message,
        }

    @staticmethod
    def _bulk_site_outcome(site, username: str, status: str, message: str) -> dict[str, str]:
        return {
            "site_id": str(site.id),
            "site": site.domain,
            "username": username,
            "status": status,
            "message": message,
        }

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

    def _validated_role(self, role: str) -> str:
        normalized_role = role.strip()
        if normalized_role not in self.ROLE_OPTIONS:
            raise ValueError("Choose a supported WordPress role.")
        return normalized_role

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
