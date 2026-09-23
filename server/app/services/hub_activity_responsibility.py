"""Shared activity ownership, delegation and visibility. Assignment never grants CRM access."""
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from app.models.customer_activity import CustomerCallActivity, CustomerMeetingActivity, CustomerTaskActivity
from app.models.hub_case import HubCase
from app.models.hub_user import HubUser
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_operations import HubOperationError

ACTIVITY_MODELS = {"call": CustomerCallActivity, "task": CustomerTaskActivity, "meeting": CustomerMeetingActivity}
ACTIVITY_VIEWS = (("mine", "Meine"), ("team", "Mein Team"), ("all", "Alle erlaubten"),
                  ("created", "Von mir erstellt"), ("unassigned", "Zuordnung erforderlich"))


@dataclass(frozen=True, kw_only=True)
class ActivityUserMetadata:
    assignee_user_id: int | None = None
    assignee_name: str = "Nicht zugeordnet"
    changed_by: str = "Nicht bekannt"
    changed_at: datetime | None = None
    completed_by: str = ""
    completed_at: datetime | None = None
    can_edit: bool = True
    can_delete: bool = True
    can_assign: bool = False


def initial_responsibility(db, actor, assignee_user_id=None):
    creator = db.scalar(select(HubUser).where(HubUser.username == actor))
    return {"created_by_user_id": creator.id if creator else None,
            "assignee_user_id": assignee_user_id if assignee_user_id is not None else
                (creator.id if creator and creator.is_active else None)}


def record_completion(activity, *, user=None, previous_status=None):
    if activity.status == "completed" and previous_status != "completed":
        activity.completed_at = datetime.now(UTC).replace(tzinfo=None)
        activity.completed_by_user_id = user.id if user else None
        activity.completed_by_name = user.display_name if user else "System"
    elif activity.status != "completed":
        activity.completed_at = None
        activity.completed_by_user_id = None
        activity.completed_by_name = None


class ActivityResponsibility:
    def __init__(self, db, user):
        self.db, self.user = db, user
        self.access = HubAccessControlService(db=db)
        self.permission = self.access.permission(role_key=user.role, module_key="activities") if user else None
        self.scope = "all" if user and user.role == "admin" else (self.permission.record_scope if self.permission else "none")
        self._users = {row.id: row for row in db.scalars(select(HubUser))}
        self._parent_ids = {}

    def right(self, action):
        return bool(self.user and self.user.is_active and
                    (self.user.role == "admin" or self.permission and getattr(self.permission, "can_" + action, False)))

    def parent_visible(self, activity):
        for key, module in (("customer_id", "customers"), ("lead_id", "leads")):
            identifier = getattr(activity, key, None)
            if identifier is not None:
                if module not in self._parent_ids:
                    self._parent_ids[module] = self.access.accessible_record_ids(user=self.user, module_key=module)
                allowed = self._parent_ids[module]
                if allowed is not None and identifier not in allowed:
                    return False
        case_id = getattr(activity, "case_id", None)
        if case_id is not None:
            case = self.db.get(HubCase, case_id)
            if case is None or not self.access.can_access_case(user=self.user, case=case):
                return False
        return True

    def same_team(self, identifier):
        other = self._users.get(identifier)
        return bool(self.user and self.user.team_id is not None and other and other.team_id == self.user.team_id)

    def visible(self, activity):
        if not self.right("view") or not self.parent_visible(activity):
            return False
        if self.scope == "all":
            return True
        if self.scope in {"assigned", "team"}:
            if self.user.id in {activity.assignee_user_id, activity.created_by_user_id}:
                return True
            return self.scope == "team" and self.same_team(activity.assignee_user_id)
        return False

    def allowed(self, activity, action="view"):
        if not self.visible(activity) or not self.right(action):
            return False
        if action == "view" or self.user.role == "admin" or activity.assignee_user_id == self.user.id:
            return True
        return self.right("manage") and (self.scope == "all" or self.scope == "team" and
            (self.same_team(activity.assignee_user_id) or activity.assignee_user_id is None and self.same_team(activity.created_by_user_id)))

    def matches(self, activity, view="all"):
        if view not in dict(ACTIVITY_VIEWS):
            raise HubOperationError("Ungueltige Aktivitaetsansicht.")
        if not self.visible(activity):
            return False
        owner = self._users.get(activity.assignee_user_id)
        return {"all": True, "mine": activity.assignee_user_id == self.user.id,
                "created": activity.created_by_user_id == self.user.id,
                "team": self.same_team(activity.assignee_user_id),
                "unassigned": owner is None or not owner.is_active}[view]

    def may_assign_to(self, other):
        return bool(other and other.is_active and (other.id == self.user.id or self.right("manage") and
            (self.scope == "all" or self.scope == "team" and self.same_team(other.id))))

    def assignee(self, raw, activity=None):
        if not isinstance(raw, str) or not raw.isdecimal() or len(raw) > 18:
            raise HubOperationError("Bitte eine verantwortliche Person auswaehlen.")
        other = self._users.get(int(raw))
        unchanged = activity is not None and activity.assignee_user_id == int(raw)
        if other is None or not other.is_active or (not unchanged and not self.may_assign_to(other)):
            raise HubOperationError("Diese Person kann nicht als verantwortlich zugewiesen werden.")
        target = ActivityResponsibility(self.db, other)
        if target.scope == "none" or not target.right("view") or not target.right("edit") or (activity is not None and not target.parent_visible(activity)):
            raise HubOperationError("Die verantwortliche Person hat keinen ausreichenden Zugriff auf die Aktivitaet oder den verknuepften Kunden, Lead bzw. Fall. Die Zuweisung erweitert keine Rechte.")
        return other

    def choices(self):
        if not self.right("view"):
            return ()
        return tuple({"id": row.id, "name": row.display_name} for row in sorted(self._users.values(), key=lambda row: row.display_name.casefold())
                     if self.may_assign_to(row) and self.access.can(row, "activities", "view") and self.access.can(row, "activities", "edit")
                     and (row.role == "admin" or self.access.permission(role_key=row.role, module_key="activities").record_scope != "none"))

    def ui_context(self):
        return {"activity_assignees": self.choices(), "activity_default_assignee": self.user.id,
                "activity_views": ACTIVITY_VIEWS, "activity_can_create": self.right("create")}

    def flags(self, activity):
        return {"can_edit": self.allowed(activity, "edit"), "can_delete": self.allowed(activity, "delete"),
                "can_assign": self.allowed(activity, "edit") and self.right("manage")}

    def filter_views(self, kind, entries, view="all"):
        from dataclasses import replace
        if not entries or not self.right("view"):
            return ()
        model = ACTIVITY_MODELS[kind]
        rows = {row.id: row for row in self.db.scalars(select(model).where(model.id.in_([entry.id for entry in entries]))) if self.matches(row, view)}
        return tuple(replace(entry, **self.flags(rows[entry.id])) for entry in entries if entry.id in rows)
