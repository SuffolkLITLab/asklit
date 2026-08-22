# AskLit

AskLit is a "no-code" factory for publishing high-quality AI chatbots grounded in your own documents. It is designed to be deployed to **Streamlit Community Cloud** in minutes by anyone, regardless of technical expertise.

**New to GitHub, Streamlit, or knowledge-base authoring?** Follow the illustrated [beginner deployment guide](docs/deploy-to-streamlit/README.md). It explains how to write a prompt, prepare PDF/DOCX source files, use the scaffolder, keep secrets out of GitHub, and deploy the finished app. You can also see the [working Tulane demonstration repository](https://github.com/nonprofittechy/tulane-asklit-demo) and [live app](https://tulane-asklit-demo.streamlit.app/).

For a more robust deployment, you can deploy it to fly.io or another inexpensive hosting option.

## 🚀 Quick Start: The Scaffolder

The easiest way to get started is using the **[AskLit Project Scaffolder](https://suffolklitlab.org/asklit)**. It is one five-step workflow: add a knowledge base, write a prompt, try the assistant conversationally, measure it against gold-labeled scenarios, and only then configure branding, access control, and deployment on the way out.

### 1. Requirements
Before you start, make sure you have:
*   **GitHub Account:** To host your code.
*   **Streamlit Community Cloud Account:** To host your app (Connect it to your GitHub).
*   **LLM Provider Account:** An API key from **OpenAI**, **Anthropic**, **Google**, or **Groq**.
    *   *Note:* If you want to use a custom or local endpoint, AskLit supports any OpenAI-compatible API.

### 2. Using the Scaffolder

Steps can be visited in any order from the sidebar; nothing leaves your browser session until you choose Export.

#### 1. Knowledge
Drag and drop your PDFs, Word docs, or text files. The tool chunks and embeds them locally so you can watch the knowledge base build in real time. Files already indexed are listed, and a document whose contents you have uploaded before is skipped instead of being indexed twice. A file that cannot be read is reported by name; the rest of the batch still indexes.

#### 2. Prompt
Write the system prompt and name the knowledge base it may search. Renaming a knowledge base moves the documents already indexed under the old name, so retrieval keeps working. If a prompt points at a knowledge base with no documents, the step says so — an assistant that quietly answers from general knowledge is the hardest RAG failure for a beginner to spot. **Advanced: deployment details** holds the YAML key and the per-prompt file list; add more prompts to give the deployed chat a sidebar choice between them.

#### 3. Chat
Try the assistant conversationally. The preview sends prior turns and shows the passages each answer retrieved, so it behaves like the app you would deploy. Conversation starters appear as buttons, switching models keeps the transcript so you can compare two models on the same question, and each conversation is capped at `limits.max_preview_chat_turns` questions (12 by default) so one browser tab cannot drain a shared class budget.

#### 4. Evaluate
Create gold-labeled scenarios in an editable table, generate grounded scenarios with AI, or upload a Promptfoo-style CSV using `input` (or `question`/`query`) and `__expected` columns. Run all scenarios against one model or use matrix mode to cross scenarios, prompts, knowledge bases, and models. Results appear in a sortable, filterable table with answers, grades, sources, latency, and approximate tokens. Experiment history is not added to the exported app.

*   Plain `__expected` values use exact matching. The lab also evaluates `contains:`, `icontains:`, `contains-any:`, `icontains-any:`, `contains-all:`, and `icontains-all:` assertions. Other Promptfoo assertion types remain visible and are marked ungraded.
*   For more flexible grading, use `llm-rubric:your criteria here` in `__expected`, or add shared rules in the advanced rubric panel. The lab asks the selected judge model for a JSON score plus narrative rationale (0.70 passes), highlights passing/failing rows, and summarizes pass rate by prompt × model. The judge sees the retrieved passages, so a rule about staying grounded in the sources can actually be checked. Judge calls are additional model calls that are logged and counted separately, so begin with a small scenario set.
*   Example: `llm-rubric:Explains the next practical step, stays grounded in the retrieved passages, and acknowledges uncertainty`. Use exact or `icontains:` labels when a required phrase must appear; use a rubric when equivalent wording should receive credit.
*   For criteria that apply across a whole scenario set, open **Advanced: shared rules for every scenario** and add one shared rule per line. Shared rules are saved in workspace YAML and combined with any row-level `llm-rubric:` labels. **A row that has both a gold label and shared rules passes only when both graders pass** — a shared rubric never overrides the assertion you wrote.
*   After a run, **Carry a result forward** applies the winning prompt × model combination to the exported app, defaulting to the highest pass rate.
*   Download the complete, unfiltered evaluation table as CSV after a run.
*   AskLit identifies Azure AI and APIM URLs even when they use the `openai` provider alias. For small OpenAI-compatible `/models` responses, the lab shows the returned models directly. Azure `/models` responses are treated as catalog entries—not proof of deployment—so Azure experiments use the configured deployment allowlist instead.
*   For a different Azure account, set `"model.allowed_models"` in Streamlit secrets or `MODEL_ALLOWED_MODELS` in `.env` to the deployment names that account actually exposes. When that setting exists it is a hard ceiling: the scaffolder will not run a model outside it, no matter what an endpoint reports.

#### 5. Export
Every deployment setting lives here, in collapsible panels: app title and welcome message, provider/endpoint/model, access control and passwords, logo and favicon uploads, footer and links, a last look at each prompt, and a summary of what each knowledge base holds.

*   **Secrets Generator:** Copy the pre-formatted TOML block from **Deployment settings & secrets**. Passwords entered above are hashed automatically and inserted into this block.
*   **Connect GitHub:** Click the button, enter AskLit's one-time code on GitHub, and approve repository access.
*   **Push:** Click "Create Repo & Push". This creates a public repository by default with your settings and documents pre-indexed. Select the private-repository checkbox first if your Streamlit plan supports private repositories.

At any point, open **Save or resume** in the sidebar to download a workspace YAML file. It contains app settings, prompts, knowledge-base pairings, and evaluation scenarios, but never API keys, uploaded file contents, vector indexes, or generated evaluation answers. Importing it later starts fresh isolated storage and lists the documents or branding images that need to be uploaded again.

### Protecting the steps that cost money

Uploading documents, writing prompts, and exporting a project cost the scaffolder host nothing. **Chat** and **Evaluate** bill real completions to the operator's key, so they can sit behind a shared password. Set one in Streamlit secrets and those two steps ask for it once per browser session:

```toml
SCAFFOLD_PASSWORD = "class-2026"
# or, to avoid storing the plain text, a PBKDF2 hash from the admin hash tool:
# SCAFFOLD_PASSWORD_HASH = "$pbkdf2-sha256$..."
```

When neither secret is set the gate stays open, so local development and existing deployments are unchanged. Five wrong guesses pause that browser session for a minute.

### Classroom concurrency

The hosted scaffolder isolates every browser session in its own UUID-named SQLite database, upload directory, and Chroma index. To accommodate a class of about 20 simultaneous users without overwhelming the host or model gateway, AskLit queues outbound completions at eight concurrent calls and local embedding work at two concurrent jobs by default. SQLite uses WAL mode and a busy timeout for shared diagnostic writes, and uploads are capped at 10 MB per file. Operators can tune `limits.max_concurrent_llm_calls`, `limits.llm_queue_timeout_seconds`, `limits.max_concurrent_embedding_jobs`, and `limits.embedding_queue_timeout_seconds` in configuration.

---

## 🛠️ Configuration & Secrets

Once your repository is created, you need to tell Streamlit your API keys.

### 1. Streamlit Secrets Manager
In your Streamlit Cloud dashboard, go to **Settings > Secrets** and paste your configuration.

**Basic Example:**
```toml
OPENAI_API_KEY = "sk-..."
ADMIN_ROUTE = "manage" # Your secret admin URL parameter
ADMIN_PASSWORD_HASH = "..." # Generated automatically by the scaffolder
"app.disable_admin" = "false" # Set to true to completely hide admin pages
```

### 2. Custom OpenAI Endpoints
If you are using a provider like **OpenRouter**, **Together.ai**, or a local **LM Studio** instance, you can set a custom base URL:
```toml
OPENAI_API_KEY = "your-provider-key"
OPENAI_BASE_URL = "https://openrouter.ai/api/v1"
"model.name" = "anthropic/claude-3-opus" # Use the provider's specific model string
```

The scaffolder exposes this as **Use a custom OpenAI-compatible endpoint** in
the AI Model step. It exports the URL and a placeholder key, never the
scaffolder host's own credential.

### 3. Limited Azure educator credentials

For workshops or shared trials, do not distribute an Azure AI Services account
key. Put Azure API Management in front of the Foundry endpoint and give the
cohort a quota-limited APIM subscription key. AskLit has a dedicated `azure_apim`
provider that sends this credential in the gateway subscription header.

See [Protecting AskLit with Azure API Management](docs/azure-apim-educator-gateway.md)
for the gateway policy, Azure setup, credential rotation, and verification
steps.

### 4. Branding Overrides
You can override any branding element in secrets without redeploying:
```toml
"branding.logo_url" = "https://example.com/logo.png"
"branding.footer_text" = "Custom Footer"
"branding.hide_asklit_badge" = "false"
```

---

## 🏗️ Advanced: Local Development

### Multiple Prompt / Knowledge Base Pairings

AskLit discovers every `.yml`, `.yaml`, and `.md` file under `prompts/`. If more than one prompt file exists, the chat sidebar shows a radio button for each prompt. YAML prompt files can connect a prompt to a knowledge base and, optionally, to a specific list of filenames:

```yaml
label: Housing
knowledgebase:
  name: housing
  files:
    - eviction_guide.pdf
    - repairs.md

prompt: |
  You answer housing questions using the connected knowledge base.

conversation starters:
  - What should I know before court?
```

Leaving `files` empty connects the prompt to every indexed document in that knowledge base. Admins can edit prompt text, knowledge base names, and connected file lists after deployment.

If you are a developer and want to run or modify AskLit locally:

1.  **Setup Environment:**
    ```bash
    pip install -r requirements.txt
    cp .streamlit/secrets.toml.example .streamlit/secrets.toml
    ```
2.  **Run:**
    ```bash
    streamlit run app.py
    ```
3.  **Admin Access:** Visit `http://localhost:8501/?manage` (matching your `ADMIN_ROUTE`).

## 📚 Repository Layout

```text
app.py                         Navigation entrypoint
scaffold.py                    Scaffolder entrypoint: routes steps, applies the access gate
chat_ui.py                     The public chat interface
admin/                         Management tools (Settings, Knowledge Base, Logs)
asklit/                        Core logic (RAG, LLM, Ingestion)
asklit/scaffold/               The scaffolder, one module per step:
  step_knowledge.py              1. upload and index documents
  step_prompt.py                 2. write the prompt and pair a knowledge base
  step_chat.py                   3. conversational preview
  step_evaluate.py               4. scenarios, matrix runs, and grading
  step_export.py                 5. deployment settings, ZIP, and GitHub push
  access.py                      password gate for the billed steps
  bundle.py                      build the deployable runtime
  config.py                      workspace configuration shapes
  endpoints.py                   model discovery and the host allowlist
  evaluation.py                  the evaluation runner (no Streamlit)
  knowledge.py                   indexing, deduplication, renaming
  ui.py                          shared widgets and step navigation
  workspace.py                   save/resume YAML
config/defaults.toml           Default settings (overridden by DB and Secrets)
data/                          Your pre-indexed knowledge base (SQLite + Chroma)
```

## ⚖️ License & Attribution

AskLit is a project of the **Suffolk University Law School [LIT Lab](https://suffolklitlab.org)**.

[Made with AskLit](https://suffolklitlab.org/asklit)
