# Hosted AskLit Studio

AskLit Studio adds persistent authoring and a public JSON chat API alongside the
existing Streamlit scaffolder. It does not replace `app.py`, `scaffold.py`, or
standalone generated apps.

The backend uses FastAPI, Jinja2 and PostgreSQL. An independent worker indexes
uploads, runs evaluations and builds Streamlit exports. API and worker processes
share PostgreSQL and either a persistent object directory or private Supabase Storage. Each worker handles one job
at a time; scale worker processes to increase background concurrency.

## Fast local development: SQLite and fixed credentials

No PostgreSQL or SSO registration is needed. From `~/asklit`, activate your Python
virtual environment, install `requirements-hosted.txt`, then:

```sh
cp -n .env.hosted.dev.example .env.hosted
python -m asklit.hosted init-db
uvicorn asklit.hosted.app:create_app --factory --reload --port 8000 --no-access-log
```

In another terminal with the same virtual environment:

```sh
python -m asklit.hosted.worker
```

Open `http://localhost:8000` and sign in with **dev@asklit.local / asklit**.
You can override these with `ASKLIT_DEV_USERNAME` and `ASKLIT_DEV_PASSWORD`.
Set `OPENAI_API_KEY` for preview/evaluation/chat; the development example uses
`gpt-4.1-nano`. Editing, organization management and saving do not need a model key.
If `.env.hosted` already exists, update it using the development example; `cp -n`
keeps your existing settings.

`ASKLIT_DEV_MODE=true` enables the local login and permits SQLite. Without an
explicit database URL, development mode defaults to
`sqlite:///hosted-data/hosted.sqlite3`. This is separate from Streamlit's databases.
Use one API process and one worker for local development. SQLite uses WAL, a busy
timeout and serialized short transactions to protect job claims and budgets;
PostgreSQL remains the deployment database for concurrent replicas. Development
mode requires a loopback `ASKLIT_BASE_URL`; keep the API bound to localhost.

## Choose a Compose database

Docker Compose 2.20+ supports the optional PostgreSQL dependency used here.
Set the following in `.env.hosted`, then use the normal Compose command below:

| Database | `COMPOSE_PROFILES` | `ASKLIT_DATABASE_URL` | `ASKLIT_DEV_MODE` |
| --- | --- | --- | --- |
| Bundled PostgreSQL | `postgres` | Leave unset; Compose builds the internal URL | `true` for local credentials, otherwise `false` |
| SQLite | Empty | `sqlite:////var/lib/asklit/hosted.sqlite3` | `true` |
| External PostgreSQL/Supabase | Empty | Your `postgresql+psycopg://…` URL | `false` for deployment |

SQLite's database and documents both persist in the `objects` volume. The
migration container mounts that volume too. With the PostgreSQL profile disabled,
Compose starts only migrations, API and worker. If switching away from an already
running PostgreSQL stack, run `docker compose --env-file .env.hosted -f compose.hosted.yml down`
before changing profiles. Switching databases does not copy existing bots.

For a disposable Docker SQLite trial without editing `.env.hosted`:

```sh
ASKLIT_ENV_FILE=.env.hosted.dev.example \
ASKLIT_DATABASE_URL=sqlite:////var/lib/asklit/hosted.sqlite3 \
docker compose --env-file .env.hosted.dev.example -f compose.hosted.yml up --build -d
```

Use `.env.hosted` for regular work and your own model key. `ASKLIT_ENV_FILE` is an
optional override for the containers' environment file; `--env-file` controls
Compose variable interpolation.

## Start locally with PostgreSQL and SSO

Use Python 3.12 or newer and PostgreSQL 17. From this repository:

```sh
pip install -r requirements-hosted.txt
cp .env.hosted.example .env.hosted
# Edit .env.hosted: set independent random secrets, database URL, SSO and model credentials.
python -m asklit.hosted init-db
uvicorn asklit.hosted.app:create_app --factory --port 8000 --no-access-log
```

In another terminal:

```sh
python -m asklit.hosted.worker
```

`.env.hosted` is loaded by the application and is gitignored. Existing environment
variables take precedence. Generate secrets with
`python -c 'import secrets; print(secrets.token_urlsafe(48))'`.

Open `http://localhost:8000`. With development mode disabled, use a registered
Google or Microsoft application. Without SSO configuration the
landing page explains what the operator must configure.

The database command applies Alembic migrations. Run it once before starting
replicas during an upgrade. `/health` checks database connectivity;
`/openapi.json` exposes the machine-readable API contract. Interactive Swagger
pages are disabled so the editor can use a strict script policy.

## Docker deployment

Set `.env.hosted`, including `POSTGRES_PASSWORD` (a URL-safe random value) and
`ASKLIT_SECRET_KEY`. Compose supplies the internal PostgreSQL URL.

```sh
docker compose --env-file .env.hosted -f compose.hosted.yml up --build -d
docker compose --env-file .env.hosted -f compose.hosted.yml logs -f api worker
```

The API binds to localhost port 8000. Put an HTTPS reverse proxy in front and set
`ASKLIT_BASE_URL` to the public HTTPS origin. Configure the proxy to cap request
bodies at 11 MB and allow at least 120 seconds for chat responses. Only trust
forwarded IP headers from your reverse proxy: set Uvicorn's
`FORWARDED_ALLOW_IPS` to its actual trusted address/network. Public session
creation limits use the resolved client IP. Do not enable arbitrary forwarded
headers from the internet.

Do not record authorization headers, cookies, OAuth callback query strings or
message bodies in reverse-proxy logs. The supplied Uvicorn command disables
access logs. Application audit records record actions and resource IDs, not
message text or credentials.

Back up both the PostgreSQL `database` volume and the `objects` volume together.
The `models` volume is a rebuildable embedding-model cache. Original documents,
export ZIPs, publication histories and evaluation results are retained until deliberately
removed by an operator; plan storage accordingly. An archive hides a bot and
unpublishes it, while preserving its records. It is not a permanent purge.

Multiple API processes and worker replicas are supported. Across machines,
use [Supabase private Storage](supabase-deployment.md), or point `ASKLIT_STORAGE`
to the same shared filesystem. Do not put each replica's uploads on an
independent local disk when using the filesystem backend.
PostgreSQL stores vectors with immutable document chunks; retrieval scans a
bounded corpus of at most 2,000 chunks per bot. This is suitable for small school
and court-form knowledge bases. Larger corpora need a vector-search adapter.

## SSO and organization ownership

Register Google OAuth client credentials for a web application with callback:

```text
https://YOUR-ASKLIT-HOST/auth/google/callback
```

Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. Register the corresponding
localhost URI for local development. The requested scopes are only
`openid email profile`; no Drive, Gmail or document permissions are requested.

Register a Microsoft Entra web application supporting accounts in organizational
directories, with callback:

```text
https://YOUR-ASKLIT-HOST/auth/microsoft/callback
```

Set `MICROSOFT_CLIENT_ID` and `MICROSOFT_CLIENT_SECRET`. Keep
`MICROSOFT_TENANT=organizations` for multiple schools or use a specific tenant
UUID to restrict Microsoft sign-in to one directory. Personal Microsoft accounts
are outside this work/school configuration. Only `openid email profile` scopes
are requested. Entra tenant-aware issuer validation supplements Authlib's
signature, audience, expiry, nonce, state and PKCE checks.

Users are identified by validated issuer and subject, not email. Google and
Microsoft accounts with the same email remain separate accounts; automatic
account merging is intentionally absent. A new identity gets a personal
workspace. Create a school organization and invite people into it:

- **Member:** create, edit, evaluate, publish and export their own bots.
- **Administrator:** manage all bots in that organization and invite members.
- **Owner:** additionally appoint administrators/owners and remove members.

An organization must keep at least one owner. Owners can remove a member without
losing the school's bots; administrators can transfer those bots to another
member. Membership is checked on every management request. Knowing a bot ID,
having a school email address, or belonging to an Entra tenant does not grant
organization access automatically.

Invitation links are single-use bearer capabilities, valid for seven days.
Share them directly with the intended recipient. The application displays links;
it does not send invitations by email. A recipient may sign in with either
configured SSO provider. Membership changes and authoring actions are audited.

## Model configuration and limits

`ASKLIT_MODELS_JSON` is an operator-defined map of presets. For example:

```json
{
  "classroom": {
    "provider": "azure_apim",
    "model": "YOUR-DEPLOYMENT",
    "base_url": "https://YOUR-GATEWAY.azure-api.net/asklit",
    "api_key_env": "CLASSROOM_APIM_KEY",
    "max_tokens": 1500
  }
}
```

Set the referenced key as an environment variable. Authors choose preset IDs;
they cannot upload endpoints, credentials, executable code or remote repositories
to the runtime. Keep credentials out of `base_url`: endpoints and model names
are included in standalone exports. Use a new preset ID when changing model
identity if older publications must continue using the old model. Credential
rotation can keep the same environment-variable name.

The default embedding model is `all-MiniLM-L6-v2`, downloaded on first use.
`ASKLIT_EMBEDDING_MODEL` changes the default for new knowledge bases. Existing
bots keep the embedding model of their source documents. Model loading and
embedding are local; completions and rubric grades use the selected preset.

Defaults:

| Limit | Default |
| --- | --- |
| `ASKLIT_DAILY_CALLS` | 500 model calls per organization per UTC day |
| `ASKLIT_GLOBAL_DAILY_CALLS` | 5,000 authoring calls across the host per UTC day |
| `ASKLIT_PUBLIC_DAILY_CALLS` | Separate 5,000 public-chat calls per UTC day |
| `ASKLIT_RETENTION_DAYS` | 7 days for public conversations |
| `ASKLIT_JOB_TIMEOUT_SECONDS` | 1,800 seconds per background job |
| Public session creation | 30 new conversations per IP per hour |
| Conversation length | 12 questions |
| Upload size | 10 MB per document; 100 active documents per bot |
| Evaluation size | 240 answer + rubric calls per run (`ASKLIT_EVALUATION_MAX_CALLS`) |

Calls are reserved transactionally before execution, including failed calls.
Public chat has a separate host-wide pool so self-service editor signups cannot
exhaust CourtFormsOnline's host-wide allocation. The organization limit still
applies to its combined authoring/public usage.
Evaluations reserve their whole run up front. Daily limits are model-call
ceilings, not dollar-exact billing budgets. Output is capped at 4,000 tokens per
call. Completed conversation request IDs are idempotent; failed or interrupted
IDs are never automatically replayed. Workers fail stale jobs and enforce a
hard process timeout. They do not silently rerun potentially billed work.

The worker also purges expired public conversations, capability records and
login/invitation sessions. Keep at least one worker running for retention cleanup.
Public users can explicitly delete their conversation earlier. Neither the
editor nor public API exposes public transcripts to school administrators.

## Publication and compatibility

Each publish captures configuration and exact immutable source-document IDs.
Draft edits/removals do not change existing publications. Conversations pin their
starting revision. Unpublishing immediately prevents further chat calls, even
for existing conversations. Rollback changes the active publication for new
conversations; it does not rewrite existing conversation history.

Workspace YAML v1 imports preserve prompts, scenario assertions and rubrics.
As in Streamlit, source documents must be reuploaded. The hosted model is selected
from operator presets; imported endpoints, credentials and image branding are
not adopted. Workspace export remains readable by the existing scaffolder.
Streamlit ZIP exports include independent SQLite/Chroma data and original source
files, and exclude hosted users, memberships, sessions and conversation logs.

The Evaluate tab supports a single configuration or a scenario × prompt × source
group × model matrix. Pick a separate judge model and retrieval depth under
Grading & retrieval. Model choices are server-owned presets in
`ASKLIT_MODELS_JSON`; presets may use different providers and server-only keys.
The browser cannot provide arbitrary endpoints or credentials. `/api/me` exposes
only preset IDs and display labels, never keys or endpoints.

The default per-run cap is 240 answer plus judge calls, configurable with
`ASKLIT_EVALUATION_MAX_CALLS`. Rubric grading adds one judge call per enabled row;
a 3 × 3 × 3 matrix with three questions uses 81 answers (162 calls with grading).
The organization and host daily budgets still apply. Configure worker timeout
and daily budgets for larger runs. The worker records progress and continues
through individual answer/judge failures; results preserve the queued snapshot.

Questions support CSV import/export with the scaffolder's input/expected aliases,
Promptfoo descriptions and metadata. Source-based generation adds up to 12
reviewable questions using a selected prompt and model; generation costs one
call. A saved draft may contain up to 120 questions. Results show a per-prompt,
source-group and model comparison, pass rates, answer details, sources, timing,
estimated tokens and downloadable CSV. Workspace YAML preserves evaluation
selections, scenario labels/metadata and rubric rules. Importing into a server
with different preset IDs requires matching operator presets.

## CourtFormsOnline and other static Next.js sites

See [the integration guide](../integrations/nextjs/README.md). The public frontend
requires no server routes, secrets, SSO cookies, or arbitrary executable URL.

## Verification

```sh
python -m pytest tests/hosted -q
ASKLIT_TEST_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST/TEST_DB python -m pytest tests/hosted -q
```

PostgreSQL tests create and drop isolated random schemas. The test user needs
schema-creation permission. Without that variable, unit tests use temporary
SQLite databases. The concurrency tests exercise SQLite transaction serialization
in that mode and PostgreSQL row locks when a PostgreSQL test URL is supplied.
SSO tests sign actual test ID tokens and check invalid audience, issuer, nonce,
expiry, signature and state. Real provider tenant registrations must still be
validated with their live credentials and HTTPS redirect URIs at deployment.

References: [FastAPI templates](https://fastapi.tiangolo.com/advanced/templates/),
[Google OIDC](https://developers.google.com/identity/openid-connect/openid-connect),
[Microsoft ID-token claims](https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference).

### Galleries, links, and access

Organization administrators configure the gallery, organization URL name, Google
Workspace domains, and Microsoft tenant IDs in the organization's sharing panel.
Creators configure each AskLit's URL name and access in Settings. Public URLs
always include the organization: `/school/housing-help`. The local development workspace defaults to `/lit/`. The author edits only the
final segment; the organization prefix is displayed as fixed text. Organization
administrators control their prefix separately. Older `/o/...` links redirect.
Names accept lowercase
ASCII letters, numbers, and single separating dashes (2–80 characters). Names are
unique within their scope; old names remain reserved and redirect after a rename.
Generated `org-ID` and `asklit-ID` names are reserved for their respective objects.

Choose public to list a published AskLit in an enabled organization gallery,
unlisted for link-only access, password for shared codes without accounts, or
organization for signed-in organizational access. Multiple passwords can be
added and individually disabled. Disabling a password revokes its existing
conversations; re-enabling it does not revive those conversations. Access changes
take effect immediately, independently of publishing content.

Organization access accepts explicit members with OAuth identities, configured
Google Workspace domains, or Microsoft tenant IDs. Local development login can
exercise member access when development mode is enabled. Google authorization
uses the signed hosted-domain claim; Microsoft authorization uses the signed
tenant identity, rather than trusting an email suffix.
See [Google's claims guidance](https://developers.google.com/identity/openid-connect)
and [Microsoft's claims validation guidance](https://learn.microsoft.com/en-us/entra/identity-platform/claims-validation).

Unlock tokens allow new conversations for one hour. Existing conversations remain
usable until their own expiration, subject to password revocation, access policy
changes, and—for organization access—the login session and current organization
rules. Password verification is rate-limited; successful unlocks do not consume
the failed-password budget.

The static Next.js integration supports `/asklit?org=school&slug=housing-help`
and `/asklit?org=school` for the gallery. Organization sign-in opens the hosted
site and returns access only to an explicitly configured CORS origin. Configure
Google/Microsoft OAuth credentials to test real provider sign-in; development
login covers the local handoff. Public GET requests never invoke a model.

### Chat file uploads

In Settings → File uploads, enable **Allow files in chat**, optionally add
**Instructions for uploaded files**, save, and publish. Preview uses the saved
draft; public conversations keep the settings of the publication they started
with. Example instruction: “Identify deadlines, quote the relevant passage, and
explain unfamiliar terms. Ask if the filing date is missing.”

The paperclip uploads and converts a file before the next message. Uploading
does not call a model or add the file to shared knowledge sources. Files can be
removed individually. Extracted text is sent to the configured model with later
questions, and deleted when the conversation is cleared or expires. Earlier
answers may still contain excerpts after an individual file is removed.
Authors cannot browse these attachments through Studio, exports, or galleries.

Chat limits are three current files, 5 MB per file, 40 PDF pages, and 30,000
extracted characters across the conversation. Over-limit documents are rejected,
not silently truncated. Failed attempts count toward upload rate limits:
12 per conversation per hour, 60 per client IP, and 300 per organization.
Removing a file releases its text allowance, but does not refund attempts.
Each API process allows two simultaneous conversions; each conversion has a
15-second wall timeout, 10-second CPU limit, 512 MB address-space limit, and
bounded output. Conversation leases serialize uploads, answers, and removals.

Conversion supports readable PDF text with pypdf; DOCX, PPTX, XLSX, and ODT with
[pinned AnyDoc](https://github.com/firecrawl/anydoc); UTF-8 text, Markdown and CSV;
and HTML text extraction. The AnyDoc dependency is **firecrawl-anydoc**, not the
unrelated `anydoc` package. Its hosted OCR option is disabled. Scanned PDFs and
images need OCR before upload. Password-protected files, macros, embedded Office objects (including chart workbooks),
oversized archives, and unsupported types are rejected. Office containers are
limited to 1,000 entries and 25 MB expanded. Original chat files exist only in a
temporary conversion directory and are removed after conversion.

Converters run in separate Python processes with isolated imports and a clean
environment. Linux Landlock restricts filesystem reads to Python libraries and
the conversion’s own temporary directory, and writes to that temporary directory.
Seccomp blocks sockets, process/thread creation, signals, execution, and process
inspection. Docker installs `libseccomp2`; the host kernel must support Landlock
ABI 3 (Linux 6.2 or later). Native Linux development has the same requirements.
Conversion fails closed if the kernel restrictions cannot be installed. No document conversion is delegated to a
cloud service. The model provider still receives the extracted text when answering.

The `hosted_attachments` table uses the existing conversation retention and
database cascade deletion. Migrations apply the same Supabase RLS and
PostgREST-role restrictions as all other private hosted tables. No public Storage
bucket is needed. Existing author source uploads keep their established
conversion and indexing workflow; these new converters and limits apply to
chat and Studio Preview attachments.
Standalone Streamlit behavior is unchanged; chat attachments and their settings
are features of the hosted chat and its static frontend.

## Voice

Studio Preview, hosted chat, and the static Next.js client offer browser dictation,
read-aloud, and optional hands-free conversation. Browser speech is available to
every organization. It needs a compatible browser and microphone permission on
HTTPS or localhost; recognition support varies. Browser vendors may process
speech remotely, so this is not a promise of offline processing. Normal model
usage still applies. Dictation defaults to review-before-send; hands-free sends
the transcript automatically and resumes listening after the reply finishes.
Stop, leaving the page, or hiding the tab releases the microphone. Voice sessions
pause after ten minutes or twelve automatic turns.

Paid speech is optional and disabled for organizations by default. Configure
`ASKLIT_SPEECH_BASE_URL`, `ASKLIT_SPEECH_API_KEY`, `ASKLIT_SPEECH_STT_MODEL`, and
`ASKLIT_SPEECH_TTS_MODEL` for a provider implementing `/audio/transcriptions` and
`/audio/speech`. `ASKLIT_SPEECH_KEY_HEADER` supports `Authorization` (Bearer),
`api-key`, or `Ocp-Apim-Subscription-Key`; `ASKLIT_SPEECH_VOICE` defaults to `alloy`.
Then an organization administrator can enable paid voice in organization settings
and set its daily speech-request allowance. Users explicitly select Service in
the voice controls; browser failures never automatically incur paid speech costs.

Each transcription and synthesis counts separately, including provider failures.
The host allowance defaults to 1,000 requests/day, each organization to 100, each
conversation to 24, each preview author to 60, and each IP to 30. Counters use the
shared database (PostgreSQL/Supabase or local SQLite); there are four concurrent
speech requests per API process. These are request caps, not currency budgets.
Anonymous public bots can consume the organization allowance. Reverse proxies
must only trust forwarded client-IP headers from their actual proxy addresses.

Recordings are at most 30 seconds, mono 16 kHz PCM WAV, and 1 MB. The server
validates and normalizes them before forwarding. Public synthesis only reads an
existing answer belonging to that conversation, at most 4,000 characters. Audio
is not stored; transcripts become ordinary conversation text when sent. Stopping
playback cannot refund an already-started provider request. There are no automatic
paid retries. Existing chat retention and access controls apply.

Provider model names are deliberately not hardcoded. For example, compatible
providers may offer mini transcription/TTS models, but check their lifecycle:
OpenAI currently lists `gpt-4o-mini-transcribe` for retirement February 26, 2027
and `gpt-4o-mini-tts` January 6, 2027. Realtime API replacements are not drop-in
models for this HTTP adapter. See the official
[speech-to-text](https://developers.openai.com/api/docs/guides/speech-to-text)
and [text-to-speech](https://developers.openai.com/api/docs/guides/text-to-speech)
guides and your provider's own support policy.

### Search and delete approval requests

Service operators can search **Admin → Hosting approvals** by
name, email, or account ID, and filter pending, approved, or deleted requests.
Search is case-insensitive and applies before pagination. **Delete request** uses
an in-app confirmation and removes a pending request from the active list; it
does not delete the account, usage records, or organization history, and it does
not approve hosting. Deleted requests stay deleted across sign-ins. Use
**Deleted requests → Restore request** to return one to the queue. Approved
accounts and platform operators cannot be deleted through this request action.

### School organizations and professor invitations

**Admin → Organizations** lists each organization's member count, approved
members, AI request volume, estimated AI cost, and unknown-cost calls for the
selected period. **Members, invitations & usage** shows the member roster and
per-AskLit/model usage detail. There is no author-seat limit or automatic price
calculation; these counts and costs can inform a school's hosting arrangement.

**Add organization** provisions a school with its own stable URL prefix. This
operator action works for local service administrators without adding them to the
school's membership or changing their home organization.

Create an invitation for the professor's email and choose Author, Administrator,
or Owner. An owner can manage the school's other administrators; an author can
edit only their own AskLits. Copy and share the invitation link. The app does not
send email. Links expire after seven days, can be used once, and can be revoked
from the organization's pending-invitation list.

An **Admin-issued invitation includes hosting approval**. The recipient can use
Google/Microsoft sign-in or create a local email/password account from the link.
Existing accounts must match the invited email. A new local account belongs only
to the invited school; it chooses its own password and does not need a second
approval. Existing local accounts managed by another organization must use an
OAuth identity to join a different school. OAuth identities are never merged by
email. Ordinary organization-issued invitations do not grant hosting approval.

Invitation signup is limited to named Admin-issued links. Existing members should
have role changes made through their organization's member settings, rather than
being invited again. Invalid invitation screens offer a way back to sign-in or
the dashboard. **Refresh** in the organization dialog updates the member roster,
pending invitations, and usage after someone joins.

### Closing or deleting an organization

In **Admin → Organizations**, choose **Archive** to hide a school from active
lists and pause its hosted AskLits, invitations, and new work. Existing work may
finish. Use the **Archived organizations** filter and **Restore** to reopen it;
queued tasks canceled by archiving must be started again.

Archived organizations offer **Delete permanently**. The confirmation explains
the loss of AskLits, knowledge bases, conversations, invitations, and per-school
usage history and requires the organization's name. Managed local logins are
removed; shared OAuth accounts remain. Deletion is blocked while work is running
or when managed local logins belong to platform administrators or are used by
another school. Anonymous monthly spending totals remain so deletion cannot reset
the service's AI budget. A minimal service audit records the deletion.

Private document and export keys are queued atomically with deletion. The worker
removes these files in bounded batches outside database locks, retrying storage
failures on later passes. Keep the worker running to complete cleanup.

### Open registration and trial end date

`ASKLIT_REQUIRE_AUTHOR_APPROVAL=true` is the normal production policy. An optional
`ASKLIT_OPEN_REGISTRATION_UNTIL=2027-01-01` temporarily waives it until that date
at 00:00 UTC, allowing sign-in and local **Create account** registration without
hosting approval. The end date is checked on requests and model calls; no restart
is needed when the date arrives. Signup creates a separate workspace, never joins
an existing school, and does not verify an email domain or grant operator access.

During open registration, login/terms copy reflects the trial and Admin's hosting
approval panel and approval status labels are hidden. Existing explicit approvals
are retained. Trial signup does not set permanent approval: when the trial ends,
unapproved authors need approval to edit, publish, or use hosted models. Saved
content remains. The global AI budget and ordinary usage limits stay enabled.

The Suffolk trial is configured through December 2026 using a Fly secret. Extend
it without changing code:

```sh
fly secrets set --app asklit-studio ASKLIT_OPEN_REGISTRATION_UNTIL=2027-02-01
```

For indefinite open registration, set `ASKLIT_REQUIRE_AUTHOR_APPROVAL=false`.
To restore approval immediately when the base policy remains `true`, remove the
trial override with `fly secrets unset --app asklit-studio ASKLIT_OPEN_REGISTRATION_UNTIL`.
If you previously changed the base policy to false, restore it to true as well.
Fly secret changes restart machines; the runtime date check itself needs no deploy.
Public signup uses CSRF protection, hashed passwords, and shared registration
attempt limits (10 per IP and 200 across the service per fifteen-minute window).
Local login emails remain unverified and are not used to establish school access.
