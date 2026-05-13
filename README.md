# AskLit

AskLit is a Streamlit app for publishing a simple question-answering chatbot over a curated knowledge base. It uses LiteLLM for chat models, ChromaDB for retrieval, SQLite for app state, and an optional local sentence-transformers model for embeddings.

## What You Get

- Public or shared-password chat interface
- Admin-only settings, prompt editing, usage logs, and knowledge base management
- Retrieval over PDF, DOCX, TXT, Markdown, and HTML files
- Paragraph- and sentence-aware chunking for cleaner retrieved context
- Local embeddings by default, with optional remote embedding models
- File-backed SQLite and Chroma data that can be preloaded before deployment

## Quick Start

1. Install Python 3.11 or newer.

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Create local secrets:

   ```bash
   cp .streamlit/secrets.toml.example .streamlit/secrets.toml
   ```

4. Add at least one chat model API key to `.streamlit/secrets.toml`, for example:

   ```toml
   OPENAI_API_KEY = "sk-..."
   ADMIN_ROUTE = "manage"
   ADMIN_PASSWORD_HASH = "sha256-hash-here"
   ```

5. Start the app:

   ```bash
   streamlit run app.py
   ```

6. Open the admin route with the secret query parameter:

   ```text
   http://localhost:8501/?manage
   ```

   The value after `?` must match `ADMIN_ROUTE`.

## Generate Password Hashes

AskLit stores only SHA-256 password hashes in secrets.

To generate hashes in the app:

1. Set `ADMIN_ROUTE` in `.streamlit/secrets.toml`.
2. Run `streamlit run app.py`.
3. Visit `http://localhost:8501/?YOUR_ADMIN_ROUTE`.
4. Open **Password Hash Tool**.
5. Generate values for `ADMIN_PASSWORD_HASH` and, if using shared access, `SHARED_PASSWORD_HASH`.

## Configure AskLit

Defaults live in `config/defaults.toml`. Admin UI changes are stored in `data/app.sqlite3` and override the defaults. Streamlit secrets can also override settings by using quoted dotted keys.

Common settings:

| Setting | Purpose |
| --- | --- |
| `app.title` | Browser title, navigation label, and chat page title |
| `app.welcome_message` | First assistant message shown to users |
| `app.access_mode` | `public` or `password` |
| `app.conversation_starters` | Optional list of starter prompts shown before the first user message |
| `model.provider` | LiteLLM provider such as `openai`, `azure`, `anthropic`, `google`, or `ollama` |
| `model.name` | Chat model name or Azure deployment ID |
| `model.temperature` | Sampling temperature when supported by the model |
| `model.disable_temperature` | Force temperature off for model families that reject it |
| `model.max_tokens` | Maximum visible answer tokens |
| `model.reasoning_effort` | Reasoning effort for models that support it |
| `model.use_local_embeddings` | `true` for sentence-transformers, `false` for remote embeddings |
| `model.local_embedding_model` | Local sentence-transformers model name |
| `model.embedding_model` | Remote embedding model when local embeddings are disabled |
| `retrieval.top_k` | Number of retrieved chunks to pass into the model |
| `retrieval.show_citations` | Show source snippets below answers |
| `limits.daily_request_limit` | Per-conversation daily request cap |
| `limits.daily_token_limit` | Approximate per-session daily token cap |
| `limits.max_conversation_turns` | Maximum user messages in one conversation. Defaults to `10`; set to `0` for unlimited |
| `limits.max_upload_size_mb` | Intended upload size limit for knowledge base files |
| `limits.max_prompt_length` | Intended user prompt length limit |
| `logging.enabled` | Store conversations and messages in SQLite |

Example Streamlit secrets overrides:

```toml
OPENAI_API_KEY = "sk-..."
ADMIN_ROUTE = "manage"
ADMIN_PASSWORD_HASH = "..."
SHARED_PASSWORD_HASH = "..."

"app.title" = "Tenant Help Desk"
"app.access_mode" = "password"
"app.conversation_starters" = ["What are my next steps?", "Summarize this service."]
"model.provider" = "openai"
"model.name" = "gpt-5-nano"
"model.use_local_embeddings" = "true"
"retrieval.top_k" = "5"
"limits.max_conversation_turns" = "10"
"limits.max_prompt_length" = "2000"
```

## Rate Limits And Bot Prevention

AskLit includes lightweight app-level limits:

- `limits.max_conversation_turns`: stops a chat after a fixed number of user turns. The default is `10`.
- `limits.max_prompt_length`: rejects very long user messages before calling the model.
- `limits.daily_request_limit`: limits requests per Streamlit browser session.
- `limits.daily_token_limit`: limits approximate token use per Streamlit browser session.
- `app.access_mode = "password"`: requires a shared password before the chat is usable.

For Streamlit Community Cloud, browser-session limits are useful but not sufficient bot protection. A determined bot can start fresh sessions. More durable options are:

- Use `app.access_mode = "password"` for any non-public deployment.
- Keep the admin route secret with a non-obvious `ADMIN_ROUTE`.
- Set provider-side API budgets, spend caps, and model-specific rate limits in your LLM provider account.
- Keep `limits.max_conversation_turns` low for public demos.
- Keep `limits.max_prompt_length` low enough to prevent expensive pasted documents.
- Prefer a cheaper model for public deployments.
- Put the app behind an external gateway with CAPTCHA, WAF, IP-based rate limiting, or authentication if you need a truly public, abuse-resistant app.

Streamlit Community Cloud does not expose a dependable client IP address to the app code for production-grade IP rate limiting. Treat in-app limits as cost controls and UX controls, not as a full bot mitigation layer.

## Edit The Prompt

The default prompt file is `prompts/default_system_prompt.yml`.

For a repo-level default, edit that file before deployment. If the YAML file has multiple keys, AskLit reads `prompt:` for the system prompt and `conversation starters:` for the starter cards shown before a user starts chatting. `conversation_starters:` is also accepted.

Example:

```yaml
prompt: |
  You are a helpful assistant.
  Use the provided context to answer the user's question.

conversation starters:
  - What can you help me with?
  - Summarize the knowledge base.
  - Where should I start?
```

For an admin-managed prompt, open the admin route, go to **Admin Settings**, then **Prompt Engineering**, and update the active system prompt and starter prompts. Admin prompt edits are versioned in `data/app.sqlite3`; starter prompt edits are stored in the settings table in the same database.

AskLit appends retrieved knowledge base context and retrieval instructions to your system prompt automatically. Keep the prompt focused on role, tone, scope, refusal boundaries, and how to handle uncertainty.

## Build A Knowledge Base Locally

Streamlit Community Cloud starts from your GitHub repository. Runtime writes can be lost on restart or redeploy, so the default deployment path is to build and commit the knowledge base files before deploying.

1. Run the app locally:

   ```bash
   streamlit run app.py
   ```

2. Visit the admin route.

3. Open **Knowledge Base**.

4. Upload source documents.

5. Wait for each document to finish indexing.

6. If you changed embedding settings, click **Reindex All Documents**.

7. Commit these generated paths:

   ```text
   data/app.sqlite3
   data/uploads/
   data/chroma/
   ```

The SQLite database stores document metadata, extracted chunks, settings, prompt versions, and logs. `data/uploads/` stores the original uploaded files. `data/chroma/` stores the vector index.

Important: use the same embedding configuration in production that you used when indexing. If you switch between local and remote embeddings, change the embedding model, or rename the Chroma collection, reindex before committing.

AskLit chunks documents by packing paragraphs first, then splitting oversized paragraphs on sentence boundaries, and only falling back to word boundaries for unusually long sentences. If you change the chunking code or settings, use **Reindex All Documents** before committing updated `data/` files.

## Preload Embeddings For Streamlit Community Cloud

Recommended path:

1. Keep `model.use_local_embeddings = true` unless you have a reason to pay for remote embeddings.
2. Index documents locally through the admin UI.
3. Verify answers locally.
4. Commit `data/app.sqlite3`, `data/uploads/`, and `data/chroma/`.
5. Deploy the repo to Streamlit Community Cloud with `app.py` as the entrypoint.

The local sentence-transformers model may download into `data/model_cache/` on first use. You normally do not need to commit that cache; Streamlit can download dependencies at startup. If cold starts are too slow or the model cache becomes too large, use a remote embedding model and reindex locally with `model.use_local_embeddings = false` before committing the generated Chroma data.

## Deploy On Streamlit Community Cloud

Streamlit's Community Cloud deployment uses a GitHub repository, an entrypoint file, `requirements.txt`, and a secrets panel. Official docs:

- [Deploy your app on Community Cloud](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)
- [Secrets management](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)
- [App dependencies](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies)

Deployment checklist:

1. Push your repository to GitHub.
2. Confirm `requirements.txt` is at the repository root.
3. Confirm `app.py` is the entrypoint.
4. Commit preloaded knowledge base files if you want the app to launch with documents already indexed.
5. In Streamlit Community Cloud, create a new app from the repository.
6. Set the main file path to `app.py`.
7. In **Advanced settings**, paste your secrets TOML.
8. Choose a supported Python version. Python 3.11 or 3.12 are good defaults for this app.
9. Deploy.

Do not commit `.streamlit/secrets.toml`. Commit `.streamlit/secrets.toml.example` only.

## Admin Workflow After Deployment

Use the hidden admin route:

```text
https://YOUR-APP.streamlit.app/?YOUR_ADMIN_ROUTE
```

Admin edits made in Streamlit Community Cloud are useful for testing, but treat them as temporary unless you export or reproduce them locally and commit the resulting `data/` files. For durable production changes, make the change locally, verify it, commit the updated files, and redeploy.

## Local Docker

Docker is optional and is not the default deployment path.

```bash
docker compose up -d
```

The Compose service mounts `./data` and `./.streamlit` into the container so local uploads, Chroma data, SQLite state, and secrets are reused.

## Repository Layout

```text
app.py                         Streamlit navigation entrypoint
chat_ui.py                     Public chat page
login_ui.py                    Admin login page
admin/                         Admin settings, knowledge base, logs, hash tool
asklit/                        Core app package
config/defaults.toml           Default settings
prompts/default_system_prompt.yml
data/app.sqlite3               SQLite state and preloaded metadata
data/uploads/                  Uploaded source documents
data/chroma/                   Chroma vector index
.streamlit/secrets.toml.example
requirements.txt
```

## Notes

- Reindex after changing embedding settings.
- Reindex after changing chunking behavior.
- Keep secrets out of Git.
- Commit preloaded `data/` files only when they do not contain private or sensitive source material.
- Usage logs and chat transcripts are stored in `data/app.sqlite3` when logging is enabled.
