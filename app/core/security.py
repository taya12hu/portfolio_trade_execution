"""Token encryption at rest and webhook signing."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.logging import get_logger

log = get_logger(__name__)


class TokenVault:
    """Fernet-encrypts broker tokens and connection config before they reach the database."""

    def __init__(self, key: str | None, *, allow_ephemeral: bool) -> None:
        if not key:
            if not allow_ephemeral:
                raise RuntimeError("TOKEN_ENCRYPTION_KEY is required outside dev/test")
            key = Fernet.generate_key().decode()
            log.warning(
                "vault.ephemeral_key",
                message="TOKEN_ENCRYPTION_KEY not set; using a random key. Stored broker sessions "
                "will be unreadable after a restart.",
            )
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, plaintext: str | None) -> str | None:
        if plaintext is None:
            return None
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str | None) -> str | None:
        if ciphertext is None:
            return None
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:  # wrong key or tampered value
            raise ValueError("stored secret could not be decrypted") from exc

    def encrypt_json(self, value: dict[str, Any] | None) -> str | None:
        return None if value is None else self.encrypt(json.dumps(value, sort_keys=True))

    def decrypt_json(self, ciphertext: str | None) -> dict[str, Any]:
        plaintext = self.decrypt(ciphertext)
        return json.loads(plaintext) if plaintext else {}


def sign_payload(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    return bool(signature) and hmac.compare_digest(sign_payload(secret, body), signature)


def owner_for_api_key(api_key_map: dict[str, str], presented: str | None) -> str | None:
    if not presented:
        return None
    for key, owner in api_key_map.items():
        if hmac.compare_digest(key.encode(), presented.encode()):
            return owner
    return None
