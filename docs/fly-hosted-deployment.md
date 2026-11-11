# Suffolk Fly.io deployment

The hosted service is separate from the Streamlit app in `fly.toml`.

- Studio and FastAPI: https://asklit.suffolklitlab.org
- Static CourtFormsOnline/AskLit frontend: https://asklit-chat.fly.dev/asklit
- Fly organization: `suffolk-lit-lab`
- Supabase project: `asklit-prod` (`ydmubnmfjdiroigycbro`, US East 1)
- Private object bucket: `asklit-private`

`fly.hosted.toml` runs one API process and one background worker. Releases run
`python -m asklit.hosted init-db` before updating machines. Both processes use
Supabase PostgreSQL's transaction pooler and private Storage; no persistent Fly
volume is required. Model caches are disposable. Hosted migrations enforce RLS
and remove browser-role grants on AskLit tables.

Import the server-only environment from a private file into Fly secrets:

```sh
fly secrets import --stage --app asklit-studio < /PRIVATE/PATH/production.env
fly deploy --config fly.hosted.toml --remote-only --ha=false
```

Required secrets are `ASKLIT_DATABASE_URL`, `ASKLIT_SECRET_KEY`, `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and
`ASKLIT_MODELS_JSON`. The deployed model presets use Amazon Bedrock in Suffolk
AWS account `132950722695` (local operator profile `suffolk`, never `lemmalegal`).
The runtime IAM user `asklit-bedrock-production` can only invoke the selected
models and their US inference profiles. No IAM administration, S3, or billing
permissions are granted to the running service. Rotate its access key through
IAM and update Fly secrets and private environment files together.

[Bedrock presets](bedrock-models.json) include Nova Lite, Nova Micro, Claude Haiku
4.5, and Meta Llama 3.1 8B. `tiny` uses Nova Micro with a 128-token output limit.
Legacy `gpt-4.1-mini`/`gpt-4.1` preset keys remain compatibility aliases to Bedrock
models; they no longer call OpenAI. Existing saved configurations are preserved.
The OpenAI key copied from ALWeaver has been removed from AskLit environments
and Fly. Paid speech is unavailable until a separately configured, metered
provider is added; browser speech remains available.
Optional speech settings are described in [the hosted guide](hosted-deployment.md#voice).
Paid speech requires an organization administrator's explicit opt-in and configured service credentials.

Production disables local development credentials. Configure one or both OAuth
providers using `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` and
`MICROSOFT_CLIENT_ID`/`MICROSOFT_CLIENT_SECRET`. Register these exact callbacks:

```text
https://asklit.suffolklitlab.org/auth/google/callback
https://asklit.suffolklitlab.org/auth/microsoft/callback
```

The Next.js frontend lives in the sibling `courtformsonline.org` repository.
Its `Dockerfile.asklit` builds `output: 'export'` and serves only generated files
with unprivileged nginx. No frontend runtime secrets or Node server are needed.

```sh
cd ../courtformsonline.org
fly deploy --config fly.asklit.toml --remote-only --ha=false
```

The API URL is a public build argument. The backend CORS list allows this Fly
frontend and the existing CourtFormsOnline HTTPS origins. Stable hosted AskLit
links remain on the backend; the static integration uses `/asklit?bot=BOT_ID`.
Creating a new published bot does not require rebuilding the frontend.

This deployment creates a fresh hosted database. Local development data and
credentials are not copied into production. Existing CourtFormsOnline DNS and
the separate Streamlit deployment are unaffected.

## Local accounts

Production also supports email-and-password accounts without OAuth. Organization
administrators create them under **Workspace → Members & settings → Local accounts**.
They can reset passwords and disable access. Owners assign roles using the member
controls. Disabling an account or changing its password revokes its sessions.
Accounts that own an organization must transfer/demote that ownership before
being disabled; the last-owner safeguards still apply. A person can change their
own password from the Studio header, then sign in again.

Passwords require 12–200 characters and are stored using salted scrypt hashes.
Login attempts are limited per address and client IP using shared database
counters. Local email addresses are usernames, not verified OAuth identities;
matching email text never merges a local account with a Google/Microsoft account.
Explicit organization membership admits local accounts to that organization's
restricted bots, but merely typing an allowed email domain does not.

Bootstrap the first local owner with the operator CLI after running migrations:

```sh
python -m asklit.hosted create-local-admin \
  --email ADMIN_EMAIL --name 'Lab administrator' \
  --organization 'Suffolk LIT Lab' --slug lit
```

The command prompts for a password. For automation, `--password-file` accepts a
private file; do not put passwords in shell arguments. It refuses to overwrite
an existing account or organization link. There is no public signup or password
recovery endpoint: an administrator resets lost passwords. A CLI session must
have the production database environment to bootstrap production.

Local account creation is enabled automatically for the bootstrapped lab
organization. For another trusted organization, the operator can enable it with
`python -m asklit.hosted enable-local-accounts --organization-id ID`. Newly
self-created OAuth workspaces cannot provision local accounts until enabled.
Managed local accounts belong to one organization; use OAuth accounts for
membership in multiple organizations. Newly created/reset password accounts must
choose their own password before accessing Studio. Bootstrap passwords are
operator-generated and can be changed from the header.

Fly deployments bind Uvicorn to IPv4 and use `ASKLIT_TRUST_FLY_PROXY=true` to take
client addresses from Fly's `Fly-Client-IP`, ignoring client-controlled
`X-Forwarded-For` chains. Do not enable this setting on a directly reachable
non-Fly server. Use your own trusted-proxy configuration there.

Google Cloud project `suffolk-asklit` is owned by the lab's Google account.
OAuth client creation/consent configuration is independent of Fly and Supabase.
Project IAM can grant teammates `roles/oauthconfig.editor` to manage OAuth
settings using their own Google identities.

## Primary domain and Google branding

Cloudflare DNS points `asklit.suffolklitlab.org` to
`z223kml.asklit-studio.fly.dev` using an unproxied CNAME. Fly manages HTTPS.
Keep this record DNS-only: the client-IP trust boundary currently ends at Fly.
The old Fly hostname redirects browser navigation to the primary hostname;
health checks and existing API clients remain available at the old hostname.
The static frontend is built against the primary API address.

In Google Auth Platform for project `suffolk-asklit`, edit the AskLit Studio
web client and add `https://asklit.suffolklitlab.org/auth/google/callback`
to Authorized redirect URIs. Keep the old Fly callback during transition.
No JavaScript origin is required for this server-side OAuth flow.

Set Branding application links to:

- Home page: `https://asklit.suffolklitlab.org/`
- Privacy policy: `https://asklit.suffolklitlab.org/privacy`
- Terms of service: `https://asklit.suffolklitlab.org/terms`
- Authorized domain: `suffolklitlab.org` (without scheme or subdomain)

If Google asks for domain ownership verification, verify the parent domain
through Google Search Console with its supplied DNS TXT record. Publishing the
OAuth app and completing any requested Google branding review are separate
steps; the app remains restricted to its test users until the console status
is changed. The policy pages describe the deployed service, not independently
hosted copies. Review them with the lab before publishing the OAuth app broadly.

## Hosting approval, fees, and monitoring

Production sets `ASKLIT_REQUIRE_AUTHOR_APPROVAL=true`. New OAuth identities and
administrator-created local accounts cannot author until a service operator
approves them. Sign-in, password changes, and organization-restricted public chat
remain available. The approval page explains that hosting on suffolklitlab.org
requires a small cost-recovery fee and directs requests to `litlab@suffolk.edu`.
Fees and allowances are agreed separately: this release does not automatically
charge users or provide a payment processor.

The existing local lab administrator has service-operator access. From Studio,
open **Service administration** to approve or suspend accounts and review service
usage. Organization owners/admins use **Workspace → Members & settings → Usage &
cost** for their own organization. Neither organization ownership nor creating a
local account grants approval authority. Grant another trusted service operator
explicitly with `python -m asklit.hosted set-operator --user-id USER_ID` using the
production database environment; email matching does not merge identities.
Suspension unpublishes the author's assistants and prevents new model calls;
requests already admitted may finish. Reapproval does not automatically republish.

The usage ledger stores one row before each model attempt, including public chat,
preview, scenario generation, evaluation answers, and evaluation judges. It stores
organization, AskLit, author at the time of use, model, purpose, status, reported
input/output tokens, latency, and the price snapshot used to estimate USD cost.
It does not store prompts or answers. Interrupted/failed calls without usage have
unknown cost, not zero cost. Existing transcripts cannot be used to reconstruct
past provider charges; metering starts with this release. Archived assistants
retain usage history. Model-call counts exclude non-model HTTP requests and local
embedding/conversion work. Existing organization and service daily call limits
remain enforced across workers.

Rates were checked against the AWS Price List API on 2026-11-11, using
`AmazonBedrock` and `AmazonBedrockFoundationModels` for us-east-1 on-demand input
and output tokens. Claude uses the US/geo rate ($1.10/$5.50 per million), not the
lower global rate. Other rates are in the preset JSON. Review prices when changing
models. No prompt caching is requested; reported cache use leaves cost unknown
until its pricing is supported. Estimates exclude infrastructure, taxes, discounts,
and billing adjustments. Reconcile with Suffolk AWS billing before invoicing.

Public chat pages show the backend's organization name, immutable AskLit ID,
AI-error notice, sensitive-information warning, and report link. Organization
names are supplied by publishers; they are not identity-verification badges.
