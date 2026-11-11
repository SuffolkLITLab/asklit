# Hosted AskLit architecture

Implementation design, November 2026. The existing Streamlit scaffolder and
generated standalone apps remain independent deployment options.

## Decisions

- FastAPI serves a same-origin Jinja2 editor and JSON management/public APIs.
- PostgreSQL stores identities, organizations, membership, bots, immutable
  revisions, documents/chunks/vectors, sessions, conversations, jobs, and audit
  records. SQLAlchemy provides transactions and row locks. SQLite is supported for local development with serialized short transactions.
- A creator sees their own bots; organization administrators see all bots in
  their organization. Owners manage administrator roles; members cannot grant
  themselves access by supplying a different organization ID.
- Google and Microsoft work/school OIDC identities map to users by validated
  issuer + subject. Email is a display/contact attribute, never an identity or
  automatic account-linking key. Organization membership is explicit via
  expiring, single-use invitation links; an email domain alone grants no access.
- Authlib handles code exchange, signatures, audience, expiry, state and nonce.
  Entra tenant-independent issuers need explicit tenant-aware issuer validation.
  Login uses opaque, hashed database sessions in HttpOnly cookies; management
  mutations require CSRF tokens. Temporary OAuth state uses a signed cookie.
- Model presets are operator-controlled: browser configuration chooses a preset
  ID, never a credential, provider URL, Python module, repository or arbitrary
  executable. Server environment supplies credentials.
- Publish captures an immutable configuration and immutable document IDs.
  Draft edits, document removal and failed indexing cannot mutate published
  sources. Conversations pin a revision; unpublishing disables existing chats.
- Uploaded sources use random storage keys in shared persistent storage or a private Supabase bucket. Workers
  extract and embed documents; vectors and chunks live in PostgreSQL. Retrieval
  uses cosine plus keyword ranking over a bounded corpus (2,000 chunks per bot),
  avoiding a separate vector service initially. A pgvector adapter is a future
  scale optimization, not an authorization boundary.
- Durable jobs use PostgreSQL row locks with SKIP LOCKED. Claiming and status
  updates are transactional. Workers have bounded job lifetimes; interrupted
  billed work is failed rather than silently replayed. Evaluation captures its
  configuration and source IDs at enqueue time. Indexing is idempotent.
- Calls reserve organization-wide daily allowances under a database row lock,
  including preview, public chat and rubric grading. Public chat has its own host-wide allowance, separate from authoring. Concurrent conversation
  writes are serialized, request IDs prevent duplicate completed turns, and
  timeouts fail closed. Public session creation is separately rate-limited.
- Public conversations expire and are purged; avoid putting message text or
  credentials in access/audit logs. Sources and answers are rendered as text.

## Frontend chain

CourtFormsOnline already has `output: 'export'`. A client component gets a
published bot's display metadata, POSTs to create a conversation, then POSTs
messages with an opaque conversation capability in an Authorization header.
The token stays in memory, not URLs or local storage. No Next.js API route,
server action, runtime secret or third-party cookie is required. CORS allows
configured frontend origins; it is not treated as access control. All billable
requests are POSTs and the server resolves the bot definition itself.

Existing generated pages can pass a stable bot ID into the component. A generic
statically exported `/asklit/` page can read `?bot=ID` client-side, so publishing
a new bot does not require regenerating dynamic Next.js routes. Public metadata
must omit prompts, private files, model credentials and unpublished revisions.

## Compatibility and editor

The editor supports the scaffolder's Knowledge, Prompt, Chat, Evaluate and
Export workflow with durable drafts, optimistic edit versions, publication
history/rollback and organization management. Workspace YAML v1 imports remain
supported; missing files are listed for reupload. Credentials and unsupported
custom endpoints are not imported into hosted configuration. Export supports
workspace YAML and a standalone Streamlit bundle built from hosted sources.
Core extraction, prompt assembly, deterministic/rubric grading and bundle
generation are reused rather than rewriting the existing Streamlit UI.

## Validation

Test creator/admin/outsider access on every resource; stale saves; immutable
publication and rollback; SSO issuer/identity and session/CSRF validation; upload
limits and unsupported formats; failed jobs; shared quotas; conversation
capabilities and duplicate turns; malicious config fields; import/export
compatibility; public metadata; static Next.js builds. Exercise concurrency
against PostgreSQL (SQLite cannot verify SELECT FOR UPDATE or SKIP LOCKED).
Run the existing scaffold/export/security tests and browser checks of the new
editor. Live provider credentials and an internet-facing HTTPS callback are
deployment prerequisites, not embedded development authentication bypasses.
