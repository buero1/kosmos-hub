"""Keep authentication identifiers separate from immutable attribution snapshots."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps

from sqlalchemy import select


@dataclass(frozen=True)
class RecordActor:
    user_id: int | None
    name: str
    origin: str


record_actor = ContextVar("hub_record_actor", default=None)


def resolve_record_actor(db, username=None, *, origin=None):
    from app.models.hub_user import HubUser
    from app.services.hub_activity import current_activity_request
    active = record_actor.get()
    if active is not None:
        return RecordActor(active.user_id, active.name, origin or active.origin)
    request = current_activity_request()
    if request and (not username or username == request.actor):
        if request.actor_name:
            return RecordActor(request.actor_user_id, request.actor_name, origin or request.origin)
        username = request.actor
    with db.no_autoflush:
        user = db.scalar(select(HubUser).where(HubUser.username == username)) if username else None
    return RecordActor(user.id if user else None, user.display_name if user else (username or "System"),
                       origin or (request.origin if request else "background"))


@contextmanager
def record_actor_scope(db, username=None, *, origin=None):
    token = record_actor.set(resolve_record_actor(db, username, origin=origin))
    try:
        yield
    finally:
        record_actor.reset(token)


def with_record_actor(method):
    @wraps(method)
    def scoped(self, *args, **kwargs):
        with record_actor_scope(self.db, getattr(self, "actor", None)):
            result = method(self, *args, **kwargs)
            # Some domain operations defer their flush until the caller commits.
            self.db.flush()
            return result
    return scoped
