import base64
import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.hub_setup_token import HubSetupToken
from app.models.hub_user import HubUser

_PASSWORD_ITERATIONS = 600_000
_SETUP_TOKEN_LIFETIME = timedelta(minutes=20)
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")


class HubAccountService:
    def __init__(self, *, db: Session, app_secret_key: str):
        self.db = db
        self._token_key = app_secret_key.encode("utf-8")

    def get_user(self, user_id: int) -> HubUser | None:
        return self.db.get(HubUser, user_id)

    def authenticate(self, username: str, password: str) -> HubUser | None:
        user = self.db.scalar(select(HubUser).where(HubUser.username == self.normalize_username(username)))
        if user is None or not user.is_active or not verify_password(password, user.password_hash):
            return None
        user.last_login_at = datetime.now(UTC)
        self.db.commit()
        return user

    def create_setup_token(self) -> str:
        if self.db.scalar(select(HubUser.id).limit(1)) is not None:
            raise ValueError("An administrator account already exists.")

        now = datetime.now(UTC)
        self.db.query(HubSetupToken).filter(HubSetupToken.used_at.is_(None)).delete()
        token = secrets.token_urlsafe(32)
        self.db.add(
            HubSetupToken(
                token_digest=self._digest_token(token),
                expires_at=now + _SETUP_TOKEN_LIFETIME,
            )
        )
        self.db.commit()
        return token

    def create_first_admin(self, *, token: str, username: str, password: str, password_confirmation: str) -> HubUser:
        if password != password_confirmation:
            raise ValueError("The password confirmation does not match.")
        self.validate_password(password)
        normalized_username = self.normalize_username(username)

        now = datetime.now(UTC)
        setup_token = self.db.scalar(
            select(HubSetupToken)
            .where(HubSetupToken.token_digest == self._digest_token(token))
            .where(HubSetupToken.used_at.is_(None))
            .where(HubSetupToken.expires_at > now)
            .with_for_update()
        )
        if setup_token is None:
            raise ValueError("This setup link is invalid or has expired.")
        if self.db.scalar(select(HubUser.id).limit(1)) is not None:
            raise ValueError("An administrator account already exists.")

        user = HubUser(username=normalized_username, password_hash=hash_password(password), role="admin")
        setup_token.used_at = now
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user

    def change_password(
        self,
        *,
        user: HubUser,
        current_password: str,
        new_password: str,
        password_confirmation: str,
    ) -> None:
        if not verify_password(current_password, user.password_hash):
            raise ValueError("The current password is incorrect.")
        if new_password != password_confirmation:
            raise ValueError("The password confirmation does not match.")
        self.validate_password(new_password)
        user.password_hash = hash_password(new_password)
        user.session_version += 1
        self.db.commit()

    @staticmethod
    def normalize_username(username: str) -> str:
        normalized = username.strip().lower()
        if not _USERNAME_RE.fullmatch(normalized):
            raise ValueError("Use 3-64 lowercase letters, numbers, dots, hyphens or underscores for the username.")
        return normalized

    @staticmethod
    def validate_password(password: str) -> None:
        if len(password) < 12:
            raise ValueError("Use a password with at least 12 characters.")

    def _digest_token(self, token: str) -> str:
        return hmac.new(self._token_key, token.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PASSWORD_ITERATIONS)
    return "$".join(
        [
            "pbkdf2_sha256",
            str(_PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations, encoded_salt, encoded_digest = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(encoded_salt.encode("ascii"))
        expected_digest = base64.urlsafe_b64decode(encoded_digest.encode("ascii"))
        candidate_digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate_digest, expected_digest)
