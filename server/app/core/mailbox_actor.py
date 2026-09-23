"""Propagate authenticated actors through legacy HTTP email entry points."""
from contextvars import ContextVar
from functools import wraps

mailbox_actor = ContextVar("mailbox_actor", default=None)


def resolve_mailbox_actor(actor=None):
    return mailbox_actor.get() or actor


def with_mailbox_actor(method):
    @wraps(method)
    def scoped(self, *args, **kwargs):
        token = mailbox_actor.set(resolve_mailbox_actor(kwargs.get("actor") or getattr(self, "actor", None)))
        try:
            return method(self, *args, **kwargs)
        finally:
            mailbox_actor.reset(token)
    return scoped
