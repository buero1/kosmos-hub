"""Central role, module-permission, and record-scope authorization."""

from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.models.hub_access_control import (
    HubAccessRole,
    HubRecordAccessGrant,
    HubRecordAssignment,
    HubRolePermission,
    HubTeam,
)
from app.models.customer import Customer
from app.models.hub_lead import HubLead
from app.models.hub_user import HubUser


@dataclass(frozen=True)
class AccessModule:
    key: str
    label: str
    record_scoped: bool = False
    activity_scoped: bool = False

    @property
    def scope_configurable(self):
        return self.record_scoped or self.activity_scoped


ACCESS_MODULES = (
    AccessModule("dashboard", "Dashboard"),
    AccessModule("emails", "E-Mails"),
    AccessModule("customers", "Kunden", True),
    AccessModule("contacts", "Kontakte"),
    AccessModule("leads", "Leads", True),
    AccessModule("activities", "Kalender und Aktivitäten", activity_scoped=True),
    AccessModule("cases", "Fälle"),
    AccessModule("finance", "Finance"),
    AccessModule("websites", "Websites"),
    AccessModule("agent", "Hub-Agent"),
    AccessModule("settings", "Einstellungen"),
)
ACCESS_MODULE_KEYS = frozenset(module.key for module in ACCESS_MODULES)
ACCESS_ACTIONS = ("view", "create", "edit", "delete", "export", "manage", "send")
ACCESS_SCOPES = ("none", "assigned", "team", "all")
ACCESS_SCOPE_LABELS = {
    "none": "Nur Freigaben",
    "assigned": "Eigene / zugewiesene",
    "team": "Eigenes Team",
    "all": "Alle",
}
ACCESS_ACTION_LABELS = {
    "view": "Ansehen",
    "create": "Anlegen",
    "edit": "Bearbeiten",
    "delete": "Löschen",
    "export": "Exportieren",
    "manage": "Verwalten",
    "send": "E-Mails senden",
}


def _permissions(*actions: str, scope: str = "all") -> dict[str, object]:
    return {"actions": frozenset(actions), "scope": scope}


def can_open_settings(user: HubUser | None, *, can_view: bool) -> bool:
    """The settings page remains admin-only, even with a module-level view grant."""
    return bool(user and user.is_active and user.role == "admin" and can_view)


_DEFAULT_ROLE_DEFINITIONS: dict[str, tuple[str, str, dict[str, dict[str, object]]]] = {
    "admin": (
        "Superadmin",
        "Uneingeschränkter Zugriff einschließlich Benutzer-, Rollen- und Systemeinstellungen.",
        {module.key: _permissions(*ACCESS_ACTIONS) for module in ACCESS_MODULES},
    ),
    "viewer": (
        "Lesender Zugriff",
        "Bestehende Basisrolle mit lesendem Zugriff auf Dashboard, Kunden und Websites.",
        {
            "dashboard": _permissions("view"),
            "customers": _permissions("view"),
            "websites": _permissions("view"),
        },
    ),
    "management": (
        "Teamleitung",
        "Bearbeitet operative Datensätze des eigenen Teams und kann Zuweisungen koordinieren.",
        {
            "dashboard": _permissions("view"),
            "emails": _permissions("view", "create", "edit"),
            "customers": _permissions("view", "create", "edit", "export", scope="team"),
            "contacts": _permissions("view", "create", "edit", scope="team"),
            "leads": _permissions("view", "create", "edit", "export", scope="team"),
            "activities": _permissions("view", "create", "edit", "delete", "manage", scope="team"),
            "cases": _permissions("view", "create", "edit", scope="team"),
            "finance": _permissions("view", "create", "edit", scope="team"),
        },
    ),
    "sales": (
        "Vertrieb",
        "Arbeitet mit zugewiesenen Kunden, Leads, Aktivitäten, Angeboten und E-Mails.",
        {
            "dashboard": _permissions("view"),
            "emails": _permissions("view", "create", "edit"),
            "customers": _permissions("view", "create", "edit", scope="assigned"),
            "contacts": _permissions("view", "create", "edit", scope="assigned"),
            "leads": _permissions("view", "create", "edit", scope="assigned"),
            "activities": _permissions("view", "create", "edit", scope="assigned"),
            "cases": _permissions("view", "create", "edit", scope="assigned"),
            "finance": _permissions("view", "create", "edit", scope="assigned"),
        },
    ),
    "accounting": (
        "Buchhaltung",
        "Verwaltet Finance-Datensätze und kann zugehörige Kunden lesen.",
        {
            "dashboard": _permissions("view"),
            "customers": _permissions("view"),
            "contacts": _permissions("view"),
            "finance": _permissions("view", "create", "edit", "delete", "export", "manage"),
            "emails": _permissions("view", "create"),
        },
    ),
    "technician": (
        "Technik",
        "Verwaltet Websites, Fälle und die dafür erforderlichen Kundendaten.",
        {
            "dashboard": _permissions("view"),
            "customers": _permissions("view", scope="team"),
            "contacts": _permissions("view", scope="team"),
            "cases": _permissions("view", "create", "edit", scope="team"),
            "activities": _permissions("view", "create", "edit", scope="team"),
            "websites": _permissions("view", "create", "edit", "delete", "manage"),
        },
    ),
    "employee": (
        "Mitarbeiter",
        "Bearbeitet ausschließlich eigene, zugewiesene oder ausdrücklich freigegebene Datensätze.",
        {
            "dashboard": _permissions("view"),
            "customers": _permissions("view", "edit", scope="assigned"),
            "contacts": _permissions("view", "edit", scope="assigned"),
            "leads": _permissions("view", "edit", scope="assigned"),
            "activities": _permissions("view", "create", "edit", scope="assigned"),
            "cases": _permissions("view", "create", "edit", scope="assigned"),
        },
    ),
}


class HubAccessControlService:
    def __init__(self, *, db: Session):
        self.db = db

    def ensure_defaults(self) -> None:
        existing_roles = {role.key for role in self.db.scalars(select(HubAccessRole))}
        existing_permissions = {
            (permission.role_key, permission.module_key)
            for permission in self.db.scalars(select(HubRolePermission))
        }
        for key, (name, description, module_permissions) in _DEFAULT_ROLE_DEFINITIONS.items():
            if key not in existing_roles:
                self.db.add(HubAccessRole(key=key, name=name, description=description, is_system=True))
            for module in ACCESS_MODULES:
                if (key, module.key) in existing_permissions:
                    continue
                configured = module_permissions.get(module.key, _permissions(scope="none"))
                actions = configured["actions"]
                self.db.add(HubRolePermission(
                    role_key=key,
                    module_key=module.key,
                    can_view="view" in actions,
                    can_create="create" in actions,
                    can_edit="edit" in actions,
                    can_delete="delete" in actions,
                    can_export="export" in actions,
                    can_manage="manage" in actions,
                    can_send=module.key == "emails" and ("send" in actions or "create" in actions),
                    record_scope=str(configured["scope"] if module.scope_configurable else "all"),
                ))
        non_record_modules = {module.key for module in ACCESS_MODULES if not module.scope_configurable}
        for permission in self.db.scalars(
            select(HubRolePermission).where(HubRolePermission.module_key.in_(non_record_modules))
        ):
            permission.record_scope = "all"
        self.db.flush()

    def list_roles(self) -> tuple[HubAccessRole, ...]:
        return tuple(self.db.scalars(select(HubAccessRole).where(HubAccessRole.is_active.is_(True)).order_by(HubAccessRole.name)))

    def list_teams(self) -> tuple[HubTeam, ...]:
        return tuple(self.db.scalars(select(HubTeam).where(HubTeam.is_active.is_(True)).order_by(HubTeam.name)))

    def permission_matrix(self) -> dict[str, dict[str, HubRolePermission]]:
        matrix: dict[str, dict[str, HubRolePermission]] = {}
        for permission in self.db.scalars(select(HubRolePermission)):
            matrix.setdefault(permission.role_key, {})[permission.module_key] = permission
        return matrix

    def permission(self, *, role_key: str, module_key: str) -> HubRolePermission | None:
        return self.db.scalar(
            select(HubRolePermission).where(
                HubRolePermission.role_key == role_key,
                HubRolePermission.module_key == module_key,
            )
        )

    def can(self, user: HubUser, module_key: str, action: str) -> bool:
        if user.role == "admin":
            return True
        if module_key not in ACCESS_MODULE_KEYS or action not in ACCESS_ACTIONS:
            return False
        permission = self.permission(role_key=user.role, module_key=module_key)
        return bool(permission and getattr(permission, f"can_{action}", False))

    def module_access(self, user: HubUser) -> dict[str, dict[str, object]]:
        if user.role == "admin":
            return {
                module.key: {"scope": "all", **{action: True for action in ACCESS_ACTIONS}}
                for module in ACCESS_MODULES
            }
        matrix = self.permission_matrix().get(user.role, {})
        return {
            module.key: {
                "scope": matrix[module.key].record_scope if module.key in matrix else "none",
                **{
                    action: bool(getattr(matrix[module.key], f"can_{action}")) if module.key in matrix else False
                    for action in ACCESS_ACTIONS
                },
            }
            for module in ACCESS_MODULES
        }

    def accessible_record_ids(self, *, user: HubUser, module_key: str) -> set[int] | None:
        if user.role == "admin":
            return None
        module = next((item for item in ACCESS_MODULES if item.key == module_key), None)
        permission = self.permission(role_key=user.role, module_key=module_key)
        if permission is None or not permission.can_view:
            return set()
        if module is None or not module.record_scoped:
            return None
        if permission.record_scope == "all":
            return None

        assignment_filters = []
        if permission.record_scope in {"assigned", "team"}:
            assignment_filters.append(HubRecordAssignment.owner_user_id == user.id)
        grant_filters = [HubRecordAccessGrant.user_id == user.id]
        if user.team_id is not None:
            grant_filters.append(HubRecordAccessGrant.team_id == user.team_id)
            if permission.record_scope == "team":
                assignment_filters.append(HubRecordAssignment.team_id == user.team_id)

        assignment_ids = set()
        if assignment_filters:
            assignment_ids = set(self.db.scalars(
                select(HubRecordAssignment.record_id).where(
                    HubRecordAssignment.module_key == module_key,
                    or_(*assignment_filters),
                )
            ))
        grant_ids = set(self.db.scalars(
            select(HubRecordAccessGrant.record_id).where(
                HubRecordAccessGrant.module_key == module_key,
                or_(*grant_filters),
            )
        ))
        return assignment_ids | grant_ids

    def can_access_record(self, *, user: HubUser, module_key: str, record_id: int, action: str = "view") -> bool:
        if not self.can(user, module_key, action):
            return False
        permission = self.permission(role_key=user.role, module_key=module_key)
        if user.role == "admin" or permission is None or permission.record_scope == "all":
            return True
        assignment_filters = []
        if permission.record_scope in {"assigned", "team"}:
            assignment_filters.append(HubRecordAssignment.owner_user_id == user.id)
        grant_filters = [HubRecordAccessGrant.user_id == user.id]
        if user.team_id is not None:
            grant_filters.append(HubRecordAccessGrant.team_id == user.team_id)
            if permission.record_scope == "team":
                assignment_filters.append(HubRecordAssignment.team_id == user.team_id)

        assigned = False
        if assignment_filters:
            assigned = self.db.scalar(
                select(HubRecordAssignment.id).where(
                    HubRecordAssignment.module_key == module_key,
                    HubRecordAssignment.record_id == record_id,
                    or_(*assignment_filters),
                )
            ) is not None
        if assigned:
            return True

        grant_statement = select(HubRecordAccessGrant.id).where(
            HubRecordAccessGrant.module_key == module_key,
            HubRecordAccessGrant.record_id == record_id,
            or_(*grant_filters),
        )
        if action != "view":
            grant_statement = grant_statement.where(HubRecordAccessGrant.can_edit.is_(True))
        return self.db.scalar(grant_statement) is not None

    def can_access_contact(self, *, user: HubUser, contact, action: str = "view") -> bool:
        """Contact operations inherit visibility from their linked customer."""
        return bool(
            user is not None and user.is_active and contact is not None
            and self.can(user, "contacts", "view") and self.can(user, "contacts", action)
            and (contact.customer_id is None or self.can_access_record(
                user=user, module_key="customers", record_id=contact.customer_id,
            ))
        )

    def can_access_case(self, *, user: HubUser, case, action: str = "view") -> bool:
        return bool(
            user is not None and user.is_active and case is not None
            and self.can(user, "cases", "view") and self.can(user, "cases", action)
            and (case.customer_id is None or self.can_access_record(
                user=user, module_key="customers", record_id=case.customer_id,
            ))
        )

    def assign_record(
        self,
        *,
        module_key: str,
        record_id: int,
        owner_user_id: int | None,
        team_id: int | None,
    ) -> HubRecordAssignment:
        self._validate_record_module(module_key)
        self._validate_record(module_key=module_key, record_id=record_id)
        self._validate_user(owner_user_id)
        self._validate_team(team_id)
        assignment = self.db.scalar(select(HubRecordAssignment).where(
            HubRecordAssignment.module_key == module_key,
            HubRecordAssignment.record_id == record_id,
        ))
        if assignment is None:
            assignment = HubRecordAssignment(module_key=module_key, record_id=record_id)
            self.db.add(assignment)
        assignment.owner_user_id = owner_user_id
        assignment.team_id = team_id
        self.db.flush()
        return assignment

    def assign_created_record(self, *, user: HubUser, module_key: str, record_id: int) -> HubRecordAssignment:
        return self.assign_record(
            module_key=module_key,
            record_id=record_id,
            owner_user_id=user.id,
            team_id=user.team_id,
        )

    def add_grant(
        self,
        *,
        module_key: str,
        record_id: int,
        user_id: int | None,
        team_id: int | None,
        can_edit: bool,
    ) -> HubRecordAccessGrant:
        self._validate_record_module(module_key)
        self._validate_record(module_key=module_key, record_id=record_id)
        if (user_id is None) == (team_id is None):
            raise ValueError("Wähle genau einen Benutzer oder ein Team aus.")
        self._validate_user(user_id)
        self._validate_team(team_id)
        existing = self.db.scalar(select(HubRecordAccessGrant).where(
            HubRecordAccessGrant.module_key == module_key,
            HubRecordAccessGrant.record_id == record_id,
            HubRecordAccessGrant.user_id == user_id,
            HubRecordAccessGrant.team_id == team_id,
        ))
        if existing is not None:
            existing.can_edit = can_edit
            self.db.flush()
            return existing
        grant = HubRecordAccessGrant(
            module_key=module_key,
            record_id=record_id,
            user_id=user_id,
            team_id=team_id,
            can_edit=can_edit,
        )
        self.db.add(grant)
        self.db.flush()
        return grant

    def list_assignments(self) -> tuple[HubRecordAssignment, ...]:
        return tuple(self.db.scalars(select(HubRecordAssignment).order_by(HubRecordAssignment.module_key, HubRecordAssignment.record_id)))

    def list_grants(self) -> tuple[HubRecordAccessGrant, ...]:
        return tuple(self.db.scalars(select(HubRecordAccessGrant).order_by(HubRecordAccessGrant.module_key, HubRecordAccessGrant.record_id)))

    def delete_grant(self, grant_id: int) -> None:
        grant = self.db.get(HubRecordAccessGrant, grant_id)
        if grant is None:
            raise ValueError("Diese Datensatzfreigabe wurde nicht gefunden.")
        self.db.delete(grant)
        self.db.flush()

    def create_team(self, *, name: str, description: str = "") -> HubTeam:
        normalized = " ".join(name.split())
        if not normalized or len(normalized) > 96:
            raise ValueError("Bitte einen gültigen Teamnamen eingeben.")
        if self.db.scalar(select(HubTeam.id).where(HubTeam.name == normalized)) is not None:
            raise ValueError("Ein Team mit diesem Namen besteht bereits.")
        team = HubTeam(name=normalized, description=description.strip()[:500])
        self.db.add(team)
        self.db.flush()
        return team

    def update_team(self, *, team_id: int, name: str, description: str = "") -> HubTeam:
        team = self.db.get(HubTeam, team_id)
        if team is None:
            raise ValueError("Dieses Team wurde nicht gefunden.")
        normalized = " ".join(name.split())
        if not normalized or len(normalized) > 96:
            raise ValueError("Bitte einen gültigen Teamnamen eingeben.")
        duplicate = self.db.scalar(select(HubTeam.id).where(HubTeam.name == normalized, HubTeam.id != team.id))
        if duplicate is not None:
            raise ValueError("Ein Team mit diesem Namen besteht bereits.")
        team.name = normalized
        team.description = description.strip()[:500]
        self.db.flush()
        return team

    def delete_team(self, team_id: int) -> None:
        team = self.db.get(HubTeam, team_id)
        if team is None:
            raise ValueError("Dieses Team wurde nicht gefunden.")
        if self.db.scalar(select(HubUser.id).where(HubUser.team_id == team.id).limit(1)) is not None:
            raise ValueError("Dem Team sind noch Benutzer zugewiesen.")
        self.db.execute(delete(HubRecordAccessGrant).where(HubRecordAccessGrant.team_id == team.id))
        from app.models.hub_mailbox_permission import HubMailboxPermission
        self.db.execute(delete(HubMailboxPermission).where(HubMailboxPermission.team_id == team.id))
        self.db.execute(
            HubRecordAssignment.__table__.update()
            .where(HubRecordAssignment.team_id == team.id)
            .values(team_id=None)
        )
        self.db.delete(team)
        self.db.flush()

    def save_role(
        self,
        *,
        role_key: str,
        name: str,
        description: str,
        permissions: dict[str, dict[str, object]],
    ) -> HubAccessRole:
        key = self.normalize_role_key(role_key or name)
        if key == "admin":
            raise ValueError("Die Superadmin-Rolle kann nicht eingeschränkt werden.")
        normalized_name = " ".join(name.split())
        if not normalized_name or len(normalized_name) > 96:
            raise ValueError("Bitte einen gültigen Rollennamen eingeben.")
        role = self.db.get(HubAccessRole, key)
        if role is None:
            role = HubAccessRole(key=key, name=normalized_name, description=description.strip()[:500])
            self.db.add(role)
        else:
            role.name = normalized_name
            role.description = description.strip()[:500]
        for module in ACCESS_MODULES:
            values = permissions.get(module.key, {})
            scope = str(values.get("scope") or ("all" if not module.scope_configurable else "none"))
            if scope not in ACCESS_SCOPES:
                raise ValueError("Der gewählte Datensatzbereich ist ungültig.")
            permission = self.permission(role_key=key, module_key=module.key)
            if permission is None:
                permission = HubRolePermission(role_key=key, module_key=module.key)
                self.db.add(permission)
            for action in ACCESS_ACTIONS:
                setattr(permission, f"can_{action}", bool(values.get(action)))
            permission.record_scope = scope if module.scope_configurable else "all"
        self.db.flush()
        return role

    def delete_role(self, role_key: str) -> None:
        role = self.db.get(HubAccessRole, role_key)
        if role is None:
            raise ValueError("Diese Rolle wurde nicht gefunden.")
        if role.is_system:
            raise ValueError("Diese Systemrolle kann nicht gelöscht werden.")
        if self.db.scalar(select(HubUser.id).where(HubUser.role == role_key).limit(1)) is not None:
            raise ValueError("Die Rolle ist noch Benutzern zugewiesen.")
        self.db.delete(role)
        self.db.flush()

    @staticmethod
    def normalize_role_key(value: str) -> str:
        key = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:64]
        if len(key) < 2:
            raise ValueError("Der Rollenname ist zu kurz.")
        return key

    def _validate_record(self, *, module_key: str, record_id: int) -> None:
        model = {"customers": Customer, "leads": HubLead}.get(module_key)
        if model is None or record_id < 1 or self.db.get(model, record_id) is None:
            raise ValueError("Der angegebene Datensatz wurde nicht gefunden.")

    def _validate_user(self, user_id: int | None) -> None:
        if user_id is not None and self.db.get(HubUser, user_id) is None:
            raise ValueError("Der angegebene Benutzer wurde nicht gefunden.")

    def _validate_team(self, team_id: int | None) -> None:
        if team_id is not None and self.db.get(HubTeam, team_id) is None:
            raise ValueError("Das angegebene Team wurde nicht gefunden.")

    @staticmethod
    def _validate_record_module(module_key: str) -> None:
        module = next((item for item in ACCESS_MODULES if item.key == module_key), None)
        if module is None or not module.record_scoped:
            raise ValueError("Dieses Modul unterstützt keine Datensatzfreigaben.")


def permission_target(path: str, method: str) -> tuple[str, str] | None:
    if method.upper() == "POST" and re.fullmatch(r"/finance/(?:orders|invoices)/\d+/email-compose", path):
        return "emails", "create"
    if path == "/":
        module_key = "dashboard"
    elif re.match(r"^/customers/\d+/communications/emails", path):
        module_key = "emails"
    elif re.match(r"^/customers/\d+/contacts", path):
        module_key = "contacts"
    elif re.match(r"^/customers/\d+/(?:activities|cases)", path):
        module_key = "activities" if "/activities" in path else "cases"
    elif re.match(r"^/leads/\d+/activities", path):
        module_key = "activities"
    elif path.startswith("/emails/cases"):
        module_key = "cases"
    elif path.startswith(("/emails", "/email-templates", "/email-template-folders")):
        module_key = "emails"
    elif path.startswith("/customers"):
        module_key = "customers"
    elif path.startswith("/contacts"):
        module_key = "contacts"
    elif path.startswith("/leads"):
        module_key = "leads"
    elif path.startswith(("/calendar", "/calls", "/tasks", "/meetings", "/activities")):
        module_key = "activities"
    elif path.startswith("/cases"):
        module_key = "cases"
    elif path.startswith("/finance"):
        module_key = "finance"
    elif path.startswith(("/sites", "/users", "/backups", "/updates", "/plugin-installations", "/assistant")):
        module_key = "websites"
    elif path.startswith("/agent"):
        module_key = "agent"
    elif path.startswith(("/settings", "/styling", "/module-layouts")):
        module_key = "settings"
    else:
        return None

    if re.fullmatch(r"/cases/\d+/email-links/\d+/delete", path):
        return module_key, "edit"
    if "/delete" in path or path.endswith("/delete"):
        return module_key, "delete"
    if path.startswith("/module-layouts") or any(
        part in path for part in ("/layout", "/sync", "/import", "/open-wordpress-admin")
    ):
        return module_key, "manage"
    if method.upper() in {"GET", "HEAD"}:
        if path.endswith("/new"):
            return module_key, "create"
        if "/export" in path:
            return module_key, "export"
        return module_key, "view"
    create_patterns = (
        r"^/calendar/activities$",
        r"^/customers$",
        r"^/contacts$",
        r"^/leads$",
        r"^/cases$",
        r"^/emails/cases$",
        r"^/email-template-folders$",
        r"^/email-templates/[^/]+/clone$",
        r"^/finance/[^/]+$",
        r"^/customers/\d+/(?:communications/notes|activities/[^/]+)$",
        r"^/leads/\d+/(?:notes|activities/[^/]+)$",
    )
    if any(re.fullmatch(pattern, path) for pattern in create_patterns):
        return module_key, "create"
    return module_key, "edit"


def record_target(path: str) -> tuple[str, int] | None:
    match = re.match(r"^/customers/(\d+)(?:/|$)", path)
    if match:
        return "customers", int(match.group(1))
    match = re.match(r"^/leads/(\d+)(?:/|$)", path)
    if match:
        return "leads", int(match.group(1))
    return None
