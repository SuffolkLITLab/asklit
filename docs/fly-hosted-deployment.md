# Suffolk Fly.io deployment

The hosted service is separate from the Streamlit app in `fly.toml`.

- Studio and FastAPI: https://asklit-studio.fly.dev
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
`SUPABASE_SERVICE_ROLE_KEY`, `OPENAI_API_KEY`, and `ASKLIT_MODELS_JSON`.
The model preset JSON uses direct OpenAI with no Azure endpoint override. Keep
preset keys stable because saved AskLits reference them. The `tiny` preset uses
`gpt-5.4-nano`; `gpt-4.1-mini` and `gpt-4.1` remain available presets.
Optional speech settings are described in [the hosted guide](hosted-deployment.md#voice).
Paid speech still requires an organization administrator's explicit opt-in.

Production disables local development credentials. Configure one or both OAuth
providers using `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` and
`MICROSOFT_CLIENT_ID`/`MICROSOFT_CLIENT_SECRET`. Register these exact callbacks:

```text
https://asklit-studio.fly.dev/auth/google/callback
https://asklit-studio.fly.dev/auth/microsoft/callback
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
