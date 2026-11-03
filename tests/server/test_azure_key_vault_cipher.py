from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.stores.credential_store.azure_key_vault_cipher import (
    CREDENTIAL_AZURE_KEY_NAME_ENV_VAR,
    CREDENTIAL_AZURE_KEY_VAULT_URL_ENV_VAR,
    AzureKeyVaultSecretCipher,
)
from omnigent.stores.credential_store.secret_cipher import (
    CREDENTIAL_CIPHER_ENV_VAR,
    SecretCipher,
    build_secret_cipher,
)

VAULT_URL = "https://example.vault.azure.net"
KEY_NAME = "omnigent-credentials"
KEY_ID = f"{VAULT_URL}/keys/{KEY_NAME}/version-1"
CTX_ALICE = {"workspace_id": "0", "user_id": "alice", "provider": "github", "account_id": ""}
CTX_BOB = {"workspace_id": "0", "user_id": "bob", "provider": "github", "account_id": ""}


class _FakeKeyClient:
    def __init__(self, key_id: str = KEY_ID) -> None:
        self.key_id = key_id
        self.requested_names: list[str] = []

    def get_key(self, name: str) -> Any:
        self.requested_names.append(name)
        return SimpleNamespace(id=self.key_id)


class _FakeCryptographyClient:
    def __init__(self) -> None:
        self.wrapped_keys: dict[bytes, bytes] = {}
        self.next_id = 0

    def for_key(self, key_id: str) -> _FakeCryptoOperationClient:
        return _FakeCryptoOperationClient(self, key_id)


class _FakeCryptoOperationClient:
    def __init__(self, owner: _FakeCryptographyClient, key_id: str) -> None:
        self.owner = owner
        self.key_id = key_id

    def wrap_key(self, algorithm: str, key: bytes) -> Any:
        assert algorithm == "RSA-OAEP-256"
        self.owner.next_id += 1
        wrapped = f"wrapped-{self.owner.next_id}".encode()
        self.owner.wrapped_keys[wrapped] = key
        return SimpleNamespace(encrypted_key=wrapped)

    def unwrap_key(self, algorithm: str, encrypted_key: bytes) -> Any:
        assert algorithm == "RSA-OAEP-256"
        return SimpleNamespace(key=self.owner.wrapped_keys[encrypted_key])


def _cipher(*, key_name: str = KEY_NAME) -> AzureKeyVaultSecretCipher:
    key_client = _FakeKeyClient(f"{VAULT_URL}/keys/{key_name}/version-1")
    crypto = _FakeCryptographyClient()
    return AzureKeyVaultSecretCipher(
        VAULT_URL,
        key_name,
        key_client=key_client,
        crypto_client_factory=crypto.for_key,
        wrap_algorithm="RSA-OAEP-256",
    )


def test_azure_cipher_satisfies_port_and_roundtrips_per_context() -> None:
    cipher: SecretCipher = _cipher()
    assert isinstance(cipher, SecretCipher)

    encrypted = cipher.encrypt("ghu_secret_token", context=CTX_ALICE)

    assert encrypted.startswith("osazure:")
    assert "ghu_secret_token" not in encrypted
    assert cipher.decrypt(encrypted, context=CTX_ALICE) == "ghu_secret_token"
    assert cipher.decrypt(encrypted, context=CTX_BOB) is None


def test_named_account_is_bound_into_aad() -> None:
    cipher = _cipher()
    context = {**CTX_ALICE, "account_id": "org-1"}
    encrypted = cipher.encrypt("secret", context=context)

    assert cipher.decrypt(encrypted, context=context) == "secret"
    assert cipher.decrypt(encrypted, context=CTX_ALICE) is None


def test_corrupt_or_foreign_ciphertext_returns_none() -> None:
    cipher = _cipher()

    assert cipher.decrypt("not-an-azure-envelope", context=CTX_ALICE) is None
    assert cipher.decrypt("osazure:not-base64!", context=CTX_ALICE) is None

    encrypted = cipher.encrypt("secret", context=CTX_ALICE)
    raw = base64.urlsafe_b64decode(encrypted.removeprefix("osazure:") + "===")
    corrupted = bytearray(raw)
    corrupted[-1] ^= 1
    corrupted_envelope = "osazure:" + base64.urlsafe_b64encode(corrupted).decode().rstrip("=")
    assert cipher.decrypt(corrupted_envelope, context=CTX_ALICE) is None


def test_ciphertext_from_another_configured_key_is_a_configuration_error() -> None:
    original = _cipher()
    encrypted = original.encrypt("secret", context=CTX_ALICE)
    wrong_key = _cipher(key_name="different-key")

    with pytest.raises(ValueError, match="configured Azure Key Vault key"):
        wrong_key.decrypt(encrypted, context=CTX_ALICE)


def test_explicit_key_version_is_used_to_unwrap_after_rotation() -> None:
    key_client = _FakeKeyClient(KEY_ID)
    crypto = _FakeCryptographyClient()
    cipher = AzureKeyVaultSecretCipher(
        VAULT_URL,
        KEY_NAME,
        key_client=key_client,
        crypto_client_factory=crypto.for_key,
        wrap_algorithm="RSA-OAEP-256",
    )
    encrypted = cipher.encrypt("secret", context=CTX_ALICE)
    key_client.key_id = f"{VAULT_URL}/keys/{KEY_NAME}/version-2"

    assert cipher.decrypt(encrypted, context=CTX_ALICE) == "secret"


def test_build_secret_cipher_selects_azure_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL_CIPHER_ENV_VAR, "azure_key_vault")
    monkeypatch.setenv(CREDENTIAL_AZURE_KEY_VAULT_URL_ENV_VAR, VAULT_URL)
    monkeypatch.setenv(CREDENTIAL_AZURE_KEY_NAME_ENV_VAR, KEY_NAME)

    cipher = build_secret_cipher()

    assert isinstance(cipher, AzureKeyVaultSecretCipher)


def test_azure_backend_requires_both_url_and_key_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL_CIPHER_ENV_VAR, "azure_key_vault")
    monkeypatch.setenv(CREDENTIAL_AZURE_KEY_VAULT_URL_ENV_VAR, VAULT_URL)
    monkeypatch.delenv(CREDENTIAL_AZURE_KEY_NAME_ENV_VAR, raising=False)

    with pytest.raises(ValueError, match="must both be set"):
        build_secret_cipher()
