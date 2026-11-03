"""Azure Key Vault-backed :class:`SecretCipher` using envelope encryption.

Azure Key Vault RSA operations wrap a small random data key; AES-GCM encrypts
credential payloads locally and binds each ciphertext to its row identity.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import secrets
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from omnigent.stores.credential_store.secret_cipher import SecretContext

_logger = logging.getLogger(__name__)

CREDENTIAL_AZURE_KEY_VAULT_URL_ENV_VAR = "OMNIGENT_CREDENTIAL_AZURE_KEY_VAULT_URL"
CREDENTIAL_AZURE_KEY_NAME_ENV_VAR = "OMNIGENT_CREDENTIAL_AZURE_KEY_NAME"

_ENVELOPE_PREFIX = "osazure"
_ENVELOPE_VERSION = 1
_WRAP_ALGORITHM_NAME = "RSA-OAEP-256"
_NONCE_BYTES = 12
_DATA_KEY_BYTES = 32
_KEY_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,127}$")


def build_azure_key_vault_secret_cipher() -> AzureKeyVaultSecretCipher | None:
    """Build the Azure backend from deployment config, or return ``None``.

    The Azure SDK and managed-identity credentials are loaded lazily when a
    cryptographic operation is first requested.
    """
    vault_url = os.environ.get(CREDENTIAL_AZURE_KEY_VAULT_URL_ENV_VAR, "").strip()
    key_name = os.environ.get(CREDENTIAL_AZURE_KEY_NAME_ENV_VAR, "").strip()
    if not vault_url and not key_name:
        return None
    if not vault_url or not key_name:
        raise ValueError(
            f"{CREDENTIAL_AZURE_KEY_VAULT_URL_ENV_VAR} and "
            f"{CREDENTIAL_AZURE_KEY_NAME_ENV_VAR} must both be set."
        )
    return AzureKeyVaultSecretCipher(vault_url, key_name)


def _context_aad(context: SecretContext) -> bytes:
    normalized = {key: value for key, value in context.items() if value != ""}
    return json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    raw = value.encode("ascii")
    return base64.b64decode(raw + b"=" * (-len(raw) % 4), altchars=b"-_", validate=True)


class AzureKeyVaultSecretCipher:
    """Envelope cipher using an Azure Key Vault RSA key and VM managed identity.

    The data-encryption key is random and used only in process memory. The
    Key Vault key never leaves the vault; its versioned id rides with each
    ciphertext so key rotation does not make existing rows unreadable.
    """

    def __init__(
        self,
        vault_url: str,
        key_name: str,
        *,
        credential: Any | None = None,
        key_client: Any | None = None,
        crypto_client_factory: Callable[[str], Any] | None = None,
        wrap_algorithm: Any | None = None,
    ) -> None:
        parsed = urlsplit(vault_url.rstrip("/"))
        if parsed.scheme != "https" or not parsed.netloc or parsed.path:
            raise ValueError("Azure Key Vault URL must be an HTTPS vault origin.")
        if not _KEY_NAME_RE.fullmatch(key_name):
            raise ValueError("Azure Key Vault key name has an invalid format.")
        self._vault_url = vault_url.rstrip("/")
        self._key_name = key_name
        self._credential = credential
        self._key_client = key_client
        self._crypto_client_factory = crypto_client_factory
        self._wrap_algorithm = wrap_algorithm

    @property
    def _azure_credential(self) -> Any:
        if self._credential is None:
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError as exc:  # pragma: no cover - depends on optional extra
                raise ImportError(
                    "Azure Key Vault credentials require `pip install "
                    "'omnigent[azure-key-vault]'`."
                ) from exc
            self._credential = DefaultAzureCredential()
        return self._credential

    @property
    def _keys(self) -> Any:
        if self._key_client is None:
            try:
                from azure.keyvault.keys import KeyClient
            except ImportError as exc:  # pragma: no cover - depends on optional extra
                raise ImportError(
                    "Azure Key Vault support requires `pip install 'omnigent[azure-key-vault]'`."
                ) from exc
            self._key_client = KeyClient(self._vault_url, self._azure_credential)
        return self._key_client

    def _crypto_client(self, key_id: str) -> Any:
        if self._crypto_client_factory is not None:
            return self._crypto_client_factory(key_id)
        try:
            from azure.keyvault.keys.crypto import CryptographyClient
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ImportError(
                "Azure Key Vault support requires `pip install 'omnigent[azure-key-vault]'`."
            ) from exc
        return CryptographyClient(key_id, self._azure_credential)

    @property
    def _algorithm(self) -> Any:
        if self._wrap_algorithm is not None:
            return self._wrap_algorithm
        try:
            from azure.keyvault.keys.crypto import KeyWrapAlgorithm
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ImportError(
                "Azure Key Vault support requires `pip install 'omnigent[azure-key-vault]'`."
            ) from exc
        return KeyWrapAlgorithm.rsa_oaep_256

    def _validate_key_id(self, key_id: str) -> None:
        stored = urlsplit(key_id)
        configured = urlsplit(self._vault_url)
        segments = stored.path.strip("/").split("/")
        if (
            stored.scheme.lower() != "https"
            or stored.netloc.lower() != configured.netloc.lower()
            or len(segments) != 3
            or segments[0] != "keys"
            or segments[1].lower() != self._key_name.lower()
            or not segments[2]
        ):
            raise ValueError("Stored credential uses a different configured Azure Key Vault key.")

    def encrypt(self, plaintext: str, *, context: SecretContext) -> str:
        key = self._keys.get_key(self._key_name)
        key_id = str(key.id)
        self._validate_key_id(key_id)

        data_key = secrets.token_bytes(_DATA_KEY_BYTES)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        encrypted = AESGCM(data_key).encrypt(
            nonce, plaintext.encode("utf-8"), _context_aad(context)
        )
        wrapped = self._crypto_client(key_id).wrap_key(self._algorithm, data_key).encrypted_key
        envelope = {
            "v": _ENVELOPE_VERSION,
            "key_id": key_id,
            "wrapped_key": _b64encode(bytes(wrapped)),
            "nonce": _b64encode(nonce),
            "ciphertext": _b64encode(encrypted),
        }
        encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        return f"{_ENVELOPE_PREFIX}:{_b64encode(encoded)}"

    def decrypt(self, ciphertext: str, *, context: SecretContext) -> str | None:
        prefix = f"{_ENVELOPE_PREFIX}:"
        if not ciphertext.startswith(prefix):
            return None
        try:
            envelope = json.loads(_b64decode(ciphertext[len(prefix) :]))
            if (
                not isinstance(envelope, dict)
                or type(envelope.get("v")) is not int
                or envelope["v"] != _ENVELOPE_VERSION
            ):
                raise ValueError("unsupported envelope")
            key_id = envelope["key_id"]
            encoded_fields = (
                key_id,
                envelope["wrapped_key"],
                envelope["nonce"],
                envelope["ciphertext"],
            )
            if not all(isinstance(value, str) for value in encoded_fields):
                raise ValueError("invalid envelope fields")
            wrapped_key = _b64decode(envelope["wrapped_key"])
            nonce = _b64decode(envelope["nonce"])
            encrypted = _b64decode(envelope["ciphertext"])
            if len(nonce) != _NONCE_BYTES or not wrapped_key or len(encrypted) < 16:
                raise ValueError("invalid envelope lengths")
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            binascii.Error,
            json.JSONDecodeError,
        ):
            _logger.warning(
                "credential store: malformed Azure Key Vault ciphertext; "
                "affected integration will read as disconnected"
            )
            return None

        self._validate_key_id(key_id)
        try:
            result = self._crypto_client(key_id).unwrap_key(self._algorithm, wrapped_key)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 400:
                _logger.warning(
                    "credential store: Azure Key Vault rejected wrapped data key; "
                    "affected integration will read as disconnected"
                )
                return None
            raise
        try:
            plaintext = AESGCM(bytes(result.key)).decrypt(nonce, encrypted, _context_aad(context))
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError):
            _logger.warning(
                "credential store: Azure ciphertext did not match row context or was corrupt; "
                "affected integration will read as disconnected"
            )
            return None
