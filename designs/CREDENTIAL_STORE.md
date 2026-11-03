# Per-user integration credential store

## Motivation

The GitHub App integration needs to persist a per-user OAuth token set and vend
it to managed sandboxes on demand (see the credential broker below). That secret
material must be encrypted at rest, per user, with the key never sitting in the
app process.

Two forces shape the design:

1. **More providers.** Connecting MCP integrations (Datadog, Slack, a hosted
   GitHub MCP, …) needs the same shape: a per-user secret + some non-secret
   metadata, connected once and re-vended to sandboxes. We do not want one
   bespoke table per provider.
2. **Managed key material.** Secrets use AWS KMS, HashiCorp Vault Transit, or
   Azure Key Vault. The master key stays in its service; the server receives only
   narrowly scoped cryptographic permissions. Each ciphertext is bound to its
   row identity, preventing cross-user replay.

Getting the schema generic now is cheap; reshaping a provider-specific table
after users have connected means migrating encrypted rows. Hence this store
lands at the base of the integration stack, with GitHub as its first (and,
today, only) provider.

## Two independent axes

The design deliberately separates two concerns that are easy to conflate:

| Axis | What varies | This design |
| --- | --- | --- |
| **Data model** | "a credential for *any* provider, per user" | one generic table + typed façades per provider |
| **Secret backend** | *where* the key lives and *who* encrypts | a `SecretCipher` port with KMS, Vault Transit, and Azure Key Vault adapters |

Keeping them orthogonal means a new provider is a façade (no schema change) and
a different key backend is a cipher adapter (no schema change).

## Data model

One table, `connections`:

| column | type | notes |
| --- | --- | --- |
| `workspace_id` | bigint | tenant partition, part of PK (`0` = default) |
| `user_id` | str(128) | omnigent user, part of PK |
| `provider` | str(64) | e.g. `"github"`, part of PK |
| `account_id` | str(128) | provider account discriminator, part of PK; `""` = the user's single account for that provider |
| `secret_enc` | text | Backend-specific ciphertext envelope holding *all* secret material (access token, refresh token, …) |
| `metadata_json` | text | non-secret provider metadata (login, ids, scopes, expiries, …) as JSON |
| `created_at` / `updated_at` | integer | unix epoch seconds (repo convention; `workspace_id` is the lone `bigint`) |

Primary key: `(workspace_id, user_id, provider, account_id)`.

Why a JSON secret blob rather than typed token columns: any provider's secret
shape (single API key, OAuth access+refresh, client-cert, …) fits one encrypted
column, and the ciphertext is opaque to SQL. Non-secret fields that a provider
needs at refresh time (token expiries, granted scopes, the connected login/id)
live in `metadata_json` — queryable enough for our access patterns (we always
load the whole connection) without widening the schema per provider.

The `account_id` column is `""` today (one account per provider per user) but is
in the PK so a future "connect two GitHub orgs" needs no migration.

## `SecretCipher` port

```python
class SecretCipher(Protocol):
    def encrypt(self, plaintext: str, *, context: Mapping[str, str]) -> str: ...
    def decrypt(self, ciphertext: str, *, context: Mapping[str, str]) -> str | None: ...  # None on mismatch
```

`context` is the row's identity (`workspace`/`user`/`provider`/`account`) and is
**required** — every secret is bound to a user; there is no unbound/global-key
path. The store depends on this port, not on a concrete backend, so KMS, Vault
Transit, and Azure Key Vault can be selected without a schema change.

`KmsSecretCipher` calls KMS with the row identity as its encryption context.
`VaultSecretCipher` uses a derived Transit key and the same row context.
`AzureKeyVaultSecretCipher` uses a random AES-GCM data key wrapped by an RSA key
in Key Vault; the row identity is authenticated data. Its envelope stores the
versioned Key Vault key id so key rotation preserves access to existing rows.

`build_secret_cipher()` is the single seam where a deployment selects the
backend. The chosen backend's key settings are required. If none are set, the
store and every integration on it are disabled. **There is no local-key
fallback.**

The key belongs to the **credential store**, not to any one provider: every
provider façade shares the one cipher, so connecting an MCP server or Datadog
account needs no provider-specific key and does not depend on GitHub being
configured. Provider config gates that provider's routes, not the store's
ability to encrypt.

### What the encryption context buys (and what it doesn't)

The *tenancy boundary* is the primary key and the `workspace_id`-scoped queries
— that is what stops user A reading user B's row, and it holds regardless of the
cipher. The encryption context is cryptographic binding, not an access-control
boundary. Every backend keeps the master key outside the application process
and binds ciphertext to `{workspace, user, provider, account}`.

Azure uses a fresh data key per value, AES-GCM additional authenticated data
for that identity, and Key Vault RSA-OAEP-256 to wrap the data key. The versioned
key id is stored with the envelope; retain old key versions until their rows
are re-encrypted or removed. KMS binds the same identity through its encryption
context; Vault Transit uses its derived-key context. Empty context values are
dropped consistently by all implementations.

`decrypt` returning `None` (not raising) on a ciphertext/context mismatch is
load-bearing: corrupt or wrong-context data degrades to "reconnect this
integration," never a 500 on the vend path. Operational failures — access
denied, disabled keys, or throttling — propagate instead, since reconnecting
would hide a misconfiguration.

### Pod → KMS auth (IRSA)

On the demo/prod EKS cluster the server runs as the `omnigent-server`
ServiceAccount, annotated with an IAM role (`eks.amazonaws.com/role-arn`) whose
policy allows `kms:Encrypt`/`kms:Decrypt`/`kms:DescribeKey` on the one
credential key. No AWS credentials sit in the pod; the KMS key ARN is passed as
plain config (`OMNIGENT_CREDENTIAL_KMS_KEY_ID`), not a secret. The key + role +
policy are provisioned in the infra repo (Pulumi, `cloud-infra/core`).

## Layers

The stack has four layers, each with one job:

```
route layer            provider-specific writes, uniform credential reads
   │
   ▼
ConnectionStore(ABC)   per-provider façade: maps the provider surface ⇄ a row
   │                   (GithubConnectionStore, DatabricksConnectionStore, …)
   ▼
CredentialStore        the one concrete DB backend (SQLAlchemy) — uniform writes
   │
   ▼
SecretCipher(Protocol) encrypt/decrypt the secret blob (KMS, Vault, Azure Key Vault)
```

- **`CredentialStore`** is the generic, provider-agnostic persistence: rows keyed
  by `(workspace_id, user_id, provider, account_id)`, secret blob + metadata,
  returning plain `ProviderConnection` entities.
- **`ConnectionStore(ABC)`** is the per-provider façade base. It owns a
  `CredentialStore`, carries the provider key (`_PROVIDER`), and factors the
  uniform reads (`get`, `delete`, `list_all`) so a concrete façade only sets
  `_PROVIDER`, implements `_to_entity`, and adds its provider-specific writes.
  Callers (routes, broker, launch path) talk to the façade, never the generic
  store, so nothing changes when a second provider is added.

**This base PR ships `CredentialStore` + `ConnectionStore` + `SecretCipher`; the
first concrete façade lands in the connect PR (#4235):**

- `GithubConnectionStore(ConnectionStore[GithubConnection])` — `provider="github"`;
  secret = `{access_token, refresh_token}`; metadata = `{github_login,
  github_user_id, token_expires_at, refresh_token_expires_at, scopes}`; adds
  `upsert` / `update_tokens` and implements `_to_entity`.

A future MCP-credential façade is the same pattern with `provider="mcp:<name>"`.

## Migration

This PR adds a single new migration (`add_connections`) that
`CREATE`s `connections` with the generic schema. There is no
provider-specific table in the tree to reshape — the generic store lands at the
base of the stack, before any such table existed, which is the whole point of
sequencing it first. The migration chains off the current `upstream/main` head
(the branch is kept rebased on main) so the branch and the PR merge both resolve
to a single Alembic head; a test guards that
(`tests/db/test_migration_connections.py`).

## Broker endpoint (provider-generic)

The credential broker is generic over providers:
`GET /v1/hosts/{host_id}/credentials/{provider}` resolves the launch token to
the session owner and returns that provider's secret + attribution metadata
(the git credential helper and the GitHub MCP proxy pass `provider=github`).
One route (`routes/host_credentials.py`) serves every provider that registers a
`credential_resolver` on the `ConnectionProvider` registry; a provider with no
resolver — connect-only, or on-demand delivery not built yet — returns `404`. A
new provider is one registry entry, not another hand-copied route + client. The
resolver is best-effort: a fault degrades to `{"connected": false}` rather than
a 500, and the token is never persisted in the sandbox.

## Revocation & audit note

A vended GitHub token's blast radius is bounded by the launch token: the broker
resolves `(host_id, launch_token)` server-side and stops vending the moment the
launch token expires or the host row is deleted, and the raw token never touches
sandbox disk. The selected key service can audit and revoke cryptographic
operations, but it does not know about launch tokens. So "stops vending when
the session ends" must remain a server-side check on the broker path; key
management is defence in depth on storage, not the session boundary.

## Dependencies

The credential store is backend-agnostic; each `SecretCipher` backend declares its
own extra and imports its SDK lazily:

- **AWS KMS** (`KmsSecretCipher`) needs boto3 — `omnigent[kms]`.
- **HashiCorp Vault** (`VaultSecretCipher`, Transit) needs hvac — `omnigent[vault]`.
- **Azure Key Vault** (`AzureKeyVaultSecretCipher`) needs Azure Identity and Key
  Vault SDKs — `omnigent[azure-key-vault]`.

Cloud SDKs load only when the matching backend is selected, so deployments do
not need to install unused provider SDKs.

`OMNIGENT_CREDENTIAL_CIPHER` (`kms` | `vault` | `azure_key_vault`) selects the
backend explicitly per server; its key settings are then required. Leave it unset to
auto-detect the single configured backend — configuring more than one without the
selector is an error (no silent precedence), and configuring none disables the store.

## MCP auth & roadmap

The store is the base for two credential needs; the encryption above is shared,
the delivery differs.

**MCP auth → Databricks Unity Gateway (AIGW), primary.** Routing MCP through AIGW
makes per-MCP OAuth *Databricks's* problem, not Omnigent's. The store then holds
only a small `provider="databricks"` exchange grant (per-user, KMS-encrypted);
the server performs the omni→databricks token exchange + refresh, and the
sandbox's MCP proxy fetches a short-lived AIGW token from the broker per
connection, never persisted. Leave an "MCP connection backend" seam (mirroring
the `SecretCipher` port) so a self-hosted OSS layer (e.g. Nango) can slot in
later.

**GitHub clone stays on the broker.** MCP can't `git clone`, so the GitHub App
token remains in the store and the broker vends it into the sandbox for git
(the credential-helper path); a wrapper `gh` CLI can do the Open-in-Omnigent PR
stamp instead of the MCP proxy.

### Phased plan

1. Generic per-user store + KMS cipher behind `SecretCipher`. *(this PR)*
2. KMS key + IRSA role provisioned in infra (Pulumi); wired into the demo
   deployment. *(connect PR / infra)*
3. AIGW MCP track: `provider="databricks"` + omni→databricks exchange/refresh +
   AIGW MCP proxy + the sandbox Databricks plugin behind the connection seam.

### Open questions

- **Envelope vs direct:** direct `Encrypt`/`Decrypt` is fine while blobs stay
  under 4 KB. If a provider's secret grows, switch that cipher to envelope
  (`GenerateDataKey` + local AES, wrapped key stored alongside) behind the same
  port — a per-`(context, process)` KMS call instead of per vend.
- **Encryption-context IAM condition:** whether to constrain the server's key
  policy with a `kms:EncryptionContext:*` condition (tighter, but couples the
  policy to the identity shape) or keep it key-scoped (simpler).
- **Databricks token exchange:** what the server exchanges the omni identity for
  (AIGW OAuth token / RFC 8693 token exchange / Databricks U2M/M2M grant) —
  drives the `provider=databricks` row shape and refresh cadence.
