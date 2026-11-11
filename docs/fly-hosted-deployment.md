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
