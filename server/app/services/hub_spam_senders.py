"""Exact-address spam rules shared by all inbound email sources."""

from __future__ import annotations

import re
from email.utils import parseaddr

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.hub_spam_sender import HubSpamSender


class HubSpamSenderService:
    def __init__(self, *, db: Session) -> None:
        self.db = db

    def list_senders(self) -> tuple[HubSpamSender, ...]:
        return tuple(self.db.scalars(select(HubSpamSender).order_by(HubSpamSender.created_at.desc(), HubSpamSender.id.desc())).all())

    def is_blocked(self, *, direction: str, payload: dict[str, object]) -> bool:
        if direction != "inbound" or not (address := self.sender_address(payload)):
            return False
        return self.db.scalar(select(HubSpamSender.id).where(HubSpamSender.email_address == address)) is not None

    def block(self, *, direction: str, payload: dict[str, object]) -> bool:
        if direction != "inbound" or not (address := self.sender_address(payload)):
            return False
        if self.db.scalar(select(HubSpamSender.id).where(HubSpamSender.email_address == address)) is not None:
            return False
        self.db.add(HubSpamSender(email_address=address))
        self.db.flush()
        return True

    def unblock(self, *, direction: str, payload: dict[str, object]) -> bool:
        if direction != "inbound" or not (address := self.sender_address(payload)):
            return False
        sender = self.db.scalar(select(HubSpamSender).where(HubSpamSender.email_address == address))
        if sender is None:
            return False
        self.db.delete(sender)
        self.db.flush()
        return True

    def unblock_id(self, sender_id: int) -> str | None:
        sender = self.db.get(HubSpamSender, sender_id)
        if sender is None:
            return None
        address = sender.email_address
        self.db.delete(sender)
        self.db.flush()
        return address

    @classmethod
    def sender_address(cls, payload: dict[str, object]) -> str | None:
        containers = [payload]
        for key in ("data", "payload", "record"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                containers.append(nested)
        for container in containers:
            for key in ("from", "absender", "sender", "from_address"):
                address = cls._address(container.get(key))
                if address:
                    return address
        return None

    @classmethod
    def _address(cls, value: object) -> str | None:
        if isinstance(value, dict):
            for key in ("email", "email_address", "address", "value"):
                if address := cls._address(value.get(key)):
                    return address
        elif isinstance(value, (list, tuple)):
            for item in value:
                if address := cls._address(item):
                    return address
        elif isinstance(value, str):
            address = parseaddr(value)[1].strip().casefold()
            if len(address) <= 320 and re.fullmatch(r"[^@\s]+@[^@\s]+", address):
                return address
        return None
