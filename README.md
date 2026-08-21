# AskLit

AskLit is a "no-code" factory for publishing high-quality AI chatbots grounded in your own documents. It is designed to be deployed to **Streamlit Community Cloud** in minutes by anyone, regardless of technical expertise.

**New to GitHub, Streamlit, or knowledge-base authoring?** Follow the illustrated [beginner deployment guide](docs/deploy-to-streamlit/README.md). It explains how to write a prompt, prepare PDF/DOCX source files, use the scaffolder, keep secrets out of GitHub, and deploy the finished app. You can also see the [working Tulane demonstration repository](https://github.com/nonprofittechy/tulane-asklit-demo) and [live app](https://tulane-asklit-demo.streamlit.app/).

For a more robust deployment, you can deploy it to fly.io or another inexpensive hosting option.

## 🚀 Quick Start: The Scaffolder

The easiest way to get started is using the **[AskLit Project Scaffolder](https://suffolklitlab.org/asklit)**. Start in **Playground** mode to teach or explore prompt + knowledge-base design, then use its final Export step if you want to keep and deploy the project. **Builder** mode exposes the full branding, access-control, and model-configuration workflow from the beginning.

### 1. Requirements
Before you start, make sure you have:
*   **GitHub Account:** To host your code.
*   **Streamlit Community Cloud Account:** To host your app (Connect it to your GitHub).
*   **LLM Provider Account:** An API key from **OpenAI**, **Anthropic**, **Google**, or **Groq**.
    *   *Note:* If you want to use a custom or local endpoint, AskLit supports any OpenAI-compatible API.

### 2. Using the Scaffolder
1.  **Launch the Scaffolder:** Open the "Project Scaffolder" tab in the sidebar of this app.
2.  **Identity & Branding:** 
    *   Set your app title and welcome message.
    *   **Prompt & Knowledge Base Pairings:** Define one or more prompts and the knowledge base each one should search.
    *   **Custom Branding:** Upload your own logo and favicon.
    *   *(Insert Screenshot: Scaffolder Step 1)*
3.  **AI Configuration:** Choose your model provider (e.g., OpenAI) and the model name (the default is `gpt-5.4-mini`).
4.  **Knowledge Upload:** Drag and drop your PDFs, Word docs, or Text files. The tool will chunk and embed them locally so you can see your knowledge base built in real-time.
    *   *(Insert Screenshot: Scaffolder Step 3)*
5.  **Experiment Lab:** Create gold-labeled scenarios in an editable Streamlit table, generate grounded scenarios with AI, or upload a Promptfoo-style CSV using `input` (or `question`/`query`) and `__expected` columns. Run all scenarios against one model or use matrix mode to cross scenarios, prompts, knowledge bases, and models. Results appear in a sortable, filterable table with answers, grades, sources, latency, and approximate tokens. Experiment history is not added to the exported app.
    *   Plain `__expected` values use exact matching. The lab also evaluates `contains:`, `icontains:`, `contains-any:`, `icontains-any:`, `contains-all:`, and `icontains-all:` assertions. Other Promptfoo assertion types remain visible and are marked ungraded.
    *   For more flexible grading, use `llm-rubric:your criteria here` in `__expected`. The lab asks the selected judge model for a 0–1 score (0.70 passes), showing the judge model and rationale in the results table. Judge calls are additional model calls, so begin with a small scenario set.
    *   Example: `llm-rubric:Explains the next practical step, stays grounded in the guide, and acknowledges uncertainty`. Use exact or `icontains:` labels when a required phrase must appear; use a rubric when equivalent wording should receive credit.
    *   Download the complete, unfiltered evaluation table as CSV after a run.
    *   AskLit identifies Azure AI and APIM URLs even when they use the `openai` provider alias. For small OpenAI-compatible `/models` responses, the lab shows the returned models directly. Azure `/models` responses are treated as catalog entries—not proof of deployment—so Azure experiments use the configured deployment allowlist instead.
    *   For a different Azure account, set `"model.allowed_models"` in Streamlit secrets or `MODEL_ALLOWED_MODELS` in `.env` to the deployment names that account actually exposes.
6.  **Export & Deploy:**
    *   **Connect GitHub:** Click the button, enter AskLit's one-time code on GitHub, and approve repository access.
    *   **Secrets Generator:** Copy the pre-formatted TOML block from **Deployment Settings & Secrets**. Passwords entered in the Identity step are hashed automatically and inserted into this block.
    *   **Push:** Click "Create Repo & Push". This creates a public repository by default with your settings and documents pre-indexed. Select the private-repository checkbox first if your Streamlit plan supports private repositories.

At any point, open **Save or resume** in the sidebar to download a workspace YAML file. It contains app settings, prompts, knowledge-base pairings, and evaluation scenarios, but never API keys, uploaded file contents, vector indexes, or generated evaluation answers. Importing it later starts fresh isolated storage and lists the documents or branding images that need to be uploaded again.

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
scaffold.py                    The "Project Scaffolder" wizard
chat_ui.py                     The public chat interface
admin/                         Management tools (Settings, Knowledge Base, Logs)
asklit/                        Core logic (RAG, LLM, Ingestion)
config/defaults.toml           Default settings (overridden by DB and Secrets)
data/                          Your pre-indexed knowledge base (SQLite + Chroma)
```

## ⚖️ License & Attribution

AskLit is a project of the **Suffolk University Law School [LIT Lab](https://suffolklitlab.org)**.

[Made with AskLit](https://suffolklitlab.org/asklit)
