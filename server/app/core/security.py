import base64
import hashlib
import hmac
from functools import lru_cache

from cryptography.fernet import Fernet

from app.core.config import get_settings


class SecretCipher:
    def __init__(self, secret_key: str):
        digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode("utf-8")).decode("utf-8")


def calculate_body_sha256(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def build_request_signature(site_uuid: str, timestamp: str, nonce: str, body_sha256: str, secret: str) -> str:
    message = ".".join([site_uuid, timestamp, nonce, body_sha256]).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def signatures_match(expected: str, provided: str) -> bool:
    return hmac.compare_digest(expected, provided)


@lru_cache
def get_secret_cipher() -> SecretCipher:
    settings = get_settings()
    return SecretCipher(settings.app_secret_key)

