# Supabase deployment and end-to-end verification

AskLit supports Supabase PostgreSQL (including its transaction pooler) and
private Supabase Storage. Google/Microsoft sign-in still runs through AskLit's
OIDC backend. Supabase Auth is not required, and no Supabase keys are sent to
the static Next.js frontend.

## Configure a Supabase project

1. Create a **private** bucket named `asklit-private`. Do not add public or
   authenticated-user access policies for it. Set its file-size limit high enough
   for the largest Streamlit ZIP you intend to export; ZIPs can be larger than
   individual 10 MB document uploads, and your Supabase plan's limits still apply.
2. Copy the database connection string from Supabase's **Connect** dialog. Use
   `postgresql+psycopg://` as the URL scheme. URL-encode special characters in the
   database password, and use `sslmode=require` for a hosted project.
3. Set these server-only variables in `.env.hosted`:

```dotenv
ASKLIT_DATABASE_URL=postgresql+psycopg://postgres.PROJECT:PASSWORD@POOLER_HOST:6543/postgres?sslmode=require
ASKLIT_STORAGE_BACKEND=supabase
SUPABASE_URL=https://PROJECT.supabase.co
SUPABASE_STORAGE_BUCKET=asklit-private
SUPABASE_SERVICE_ROLE_KEY=YOUR_SERVER_ONLY_SERVICE_ROLE_KEY
```

Use the project's legacy `service_role` JWT for the storage adapter. Keep it and
the database password out of browser code and `NEXT_PUBLIC_` settings. The adapter
checks that the bucket is private, streams files through temporary storage, and
serves downloads only after AskLit authorization. API and worker instances can
run on separate hosts without sharing a filesystem.

Complete the SSO/model settings from the [main deployment guide](hosted-deployment.md),
then use the Supabase Compose configuration:

```sh
docker compose -f compose.supabase.yml up --build -d
```

Alternatively run the migration, API and worker commands directly. Database
credentials must own the hosted tables or have PostgreSQL `BYPASSRLS`; they are
backend credentials, not an end-user role. AskLit disables Psycopg automatic
prepared statements to support Supabase transaction pooling. Do not use
connection-level session state to select a tenant: authorization is checked in
SQL queries and transactions for every request.

The API and Python worker still need a container/VM host. This does not deploy
FastAPI into Supabase Edge Functions. CourtFormsOnline remains independently
deployable as static files.

## Prevent Data API access from bypassing AskLit

Every online migration run reapplies the privacy guard, including to newly added
hosted tables. Migrations enable RLS on every hosted table and the dedicated
`asklit_schema_version` table, and revoke grants from `PUBLIC`, `anon` and
`authenticated`. There are no browser-access policies on these tables. All
authoring access must go through FastAPI, which applies organization and creator
permissions. A privileged service-role key can bypass RLS and must remain secret.

Alembic autogeneration is restricted to `hosted_*` tables, so it does not propose
dropping unrelated Supabase/application tables. The migration version table has
an AskLit-specific name. If you do not otherwise need Supabase's Data API, you
can also disable it for the project.

Export ZIPs and original documents currently persist until an operator removes
them. Transcript retention does not remove those objects. When pruning exports,
use the export job storage keys; do not apply a blanket bucket expiry, which would
also remove source documents needed by saved publications. Failed uploads may
leave unreferenced private objects that can be removed during storage maintenance.

## Verification performed

The local integration test used Supabase's PostgreSQL, Storage, PostgREST and
Supavisor transaction pooler, actual local sentence-transformer embeddings, and
real Azure `gpt-4.1-nano` completions capped at 128 output tokens:

- Browser: create assistant, upload/index a synthetic document, edit/save prompt,
  preview, evaluate with a rubric judge, publish, and download a Streamlit ZIP.
- Browser: static CourtFormsOnline build creates a conversation, answers with
  sources, and deletes the conversation without sending cookies.
- Changing the draft after publication does not change public answers.
- Anonymous and authenticated Supabase Data API roles cannot read hosted tables.
- Anonymous/public URLs and ordinary authenticated JWTs cannot download private
  source objects; the server storage adapter can upload and download them.
- All hosted tables have RLS enabled; schema migrations and schema checks pass.

The final automated suite passed 48 hosted checks against PostgreSQL, including
four actual Supabase integration checks. The existing Streamlit/gateway regression
suite passed 83 checks, and the Next.js chat component passed five tests. The
static production build, Docker image build/health check through the pooler, and
independent document retrieval from the exported Streamlit ZIP also passed.
Claude reviewed the implementation twice; the follow-up migration and transaction
improvements were covered by the final test run.

This verifies local Supabase compatibility, not a deployment to your cloud
project. Live Google/Microsoft registrations still need their real credentials
and redirect URIs; signed-token callback tests cover both providers separately.

## Repeat the local test

Use a disposable local Supabase project, enable its transaction pooler if testing
that path, and save `supabase status -o env` to a private file such as
`/tmp/asklit-supabase.env`. Do not print or commit that file. Create the private
bucket and run the AskLit migrations against the test database.

The opt-in fixture helper reads an existing OpenAI-compatible endpoint/key from
`.streamlit/secrets.toml` and selects `gpt-4.1-nano`. Its `--model-secrets` option
accepts another TOML file with `OPENAI_API_KEY` and optional `OPENAI_BASE_URL`.
If using the local transaction pooler, put its complete `postgresql+psycopg://`
URL in `/tmp/asklit-supabase-pooler-url` with mode 0600. Otherwise the helper uses
the direct local database URL. The helper refuses non-local Supabase endpoints.

```sh
PYTHONPATH=. python scripts/hosted_e2e_server.py bootstrap
PYTHONPATH=. python scripts/hosted_e2e_server.py api
# In another terminal:
PYTHONPATH=. python scripts/hosted_e2e_server.py worker
```

Build CourtFormsOnline with `NEXT_PUBLIC_ASKLIT_API_URL=http://127.0.0.1:8049` and
serve its `out/` directory at `http://localhost:3000`. Install Playwright and
Chromium in the test environment, then run:

```sh
python scripts/hosted_e2e_browser.py
ASKLIT_SUPABASE_TEST_ENV=/tmp/asklit-supabase.env python -m pytest tests/hosted/test_supabase.py -q
```

The browser script uses a directly seeded, synthetic test-account session; it
does not claim to test live Google/Microsoft account consent. This fixture is
separate from production startup and provides no production authentication bypass.
Stop the disposable Supabase stack and remove its test credentials after testing.

References: [Supabase database connections](https://supabase.com/docs/guides/database/connecting-to-postgres),
[prepared statements](https://supabase.com/kb/troubleshooting/disabling-prepared-statements-qL8lEL),
[Data API security](https://supabase.com/docs/guides/api/securing-your-api),
[private Storage buckets](https://supabase.com/docs/guides/storage/buckets/fundamentals).
