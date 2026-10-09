import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import ValidationError

from opsfleet_agent.core.errors import PiiValueExpiredError
from opsfleet_agent.entities.pii_tokens import TurnValue

_KEY_BYTES = 32
_NONCE_BYTES = 12


def parse_key(hex_key: str) -> bytes:
    key = bytes.fromhex(hex_key)
    if len(key) != _KEY_BYTES:
        raise ValueError("the PII value key must be 32 bytes, hex encoded")
    return key


class ValueSealer:
    def __init__(self, key: bytes) -> None:
        if len(key) != _KEY_BYTES:
            raise ValueError("the PII value key must be 32 bytes")
        self._aead = AESGCM(key)

    def seal(self, slot: str, value: TurnValue) -> bytes:
        nonce = os.urandom(_NONCE_BYTES)
        plaintext = json.dumps(
            {
                "label": value.label,
                "info_type": value.info_type,
                "value": value.value.get_secret_value(),
            }
        ).encode()
        return nonce + self._aead.encrypt(nonce, plaintext, slot.encode())

    def open(self, slot: str, sealed: bytes) -> TurnValue:
        nonce, ciphertext = sealed[:_NONCE_BYTES], sealed[_NONCE_BYTES:]
        try:
            plaintext = self._aead.decrypt(nonce, ciphertext, slot.encode())
            return TurnValue.model_validate(json.loads(plaintext))
        except (InvalidTag, ValidationError, json.JSONDecodeError) as error:
            raise PiiValueExpiredError(f"stored value for {slot} cannot be opened") from error
