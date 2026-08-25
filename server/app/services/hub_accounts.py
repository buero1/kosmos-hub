import base64
import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.hub_access_token import HubAccessToken
from app.models.hub_setup_token import HubSetupToken
from app.models.hub_user import HubUser

_PASSWORD_ITERATIONS = 600_000
_SETUP_TOKEN_LIFETIME = timedelta(minutes=20)
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_MCP_TOKEN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{2,79}$")
_MCP_TOKEN_PREFIX = "khmcp_"


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

    def list_mcp_access_tokens(self, *, user: HubUser) -> list[HubAccessToken]:
        statement = (
            select(HubAccessToken)
            .where(HubAccessToken.user_id == user.id)
            .order_by(HubAccessToken.created_at.desc())
        )
        return list(self.db.scalars(statement))

    def create_mcp_access_token(self, *, user: HubUser, name: str) -> tuple[HubAccessToken, str]:
        normalized_name = self.normalize_mcp_token_name(name)
        token = _MCP_TOKEN_PREFIX + secrets.token_urlsafe(32)
        access_token = HubAccessToken(
            user_id=user.id,
            name=normalized_name,
            token_prefix=token[:18],
            token_digest=self._digest_token(token),
        )
        self.db.add(access_token)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise ValueError("An MCP token with this name already exists.") from None
        self.db.refresh(access_token)
        return access_token, token

    def authenticate_mcp_access_token(self, token: str) -> tuple[HubUser, HubAccessToken] | None:
        if not token.startswith(_MCP_TOKEN_PREFIX) or len(token) > 256:
            return None

        access_token = self.db.scalar(
            select(HubAccessToken)
            .where(HubAccessToken.token_digest == self._digest_token(token))
            .where(HubAccessToken.revoked_at.is_(None))
        )
        if access_token is None:
            return None

        user = self.get_user(access_token.user_id)
        if user is None or not user.is_active:
            return None

        access_token.last_used_at = datetime.now(UTC)
        self.db.commit()
        return user, access_token

    def revoke_mcp_access_token(self, *, user: HubUser, token_id: int) -> HubAccessToken:
        access_token = self.db.scalar(
            select(HubAccessToken)
            .where(HubAccessToken.id == token_id)
            .where(HubAccessToken.user_id == user.id)
            .where(HubAccessToken.revoked_at.is_(None))
        )
        if access_token is None:
            raise ValueError("This active MCP token was not found.")

        access_token.revoked_at = datetime.now(UTC)
        self.db.commit()
        return access_token

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

    @staticmethod
    def normalize_mcp_token_name(name: str) -> str:
        normalized = " ".join(name.strip().split())
        if not _MCP_TOKEN_NAME_RE.fullmatch(normalized):
            raise ValueError("Use 3-80 letters, numbers, spaces, dots, hyphens or underscores for the MCP token name.")
        return normalized

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
