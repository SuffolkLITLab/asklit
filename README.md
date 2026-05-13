# AskLit

AskLit is a "no-code" factory for publishing high-quality AI chatbots grounded in your own documents. It is designed to be deployed to **Streamlit Community Cloud** in minutes by anyone, regardless of technical expertise.

For a more robust deployment, you can deploy it to fly.io or another inexpensive hosting option.

## 🚀 Quick Start: The Scaffolder

The easiest way to get started is using the **[AskLit Project Scaffolder](https://suffolklitlab.org/asklit)**. This tool allows you to build your entire app—including your knowledge base and custom branding—right in your browser, then export it directly to GitHub.

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
    *   **Custom Prompt:** Define how your AI should act (e.g., "You are a friendly librarian...").
    *   **Custom Branding:** Upload your own logo and favicon.
    *   *(Insert Screenshot: Scaffolder Step 1)*
3.  **AI Configuration:** Choose your model provider (e.g., OpenAI) and the model name (e.g., `gpt-4o`).
4.  **Knowledge Upload:** Drag and drop your PDFs, Word docs, or Text files. The tool will chunk and embed them locally so you can see your knowledge base built in real-time.
    *   *(Insert Screenshot: Scaffolder Step 3)*
5.  **Export & Deploy:** 
    *   **Connect GitHub:** Click the button to authorize AskLit to create a repo for you.
    *   **Secrets Generator:** Copy the pre-formatted TOML block from the "Deployment Secrets" box. Use the built-in **Password Hasher** if you need to set a password.
    *   **Push:** Click "Create Repo & Push". This creates a private repository on your account with all your settings and documents pre-indexed.

---

## 🛠️ Configuration & Secrets

Once your repository is created, you need to tell Streamlit your API keys.

### 1. Streamlit Secrets Manager
In your Streamlit Cloud dashboard, go to **Settings > Secrets** and paste your configuration.

**Basic Example:**
```toml
OPENAI_API_KEY = "sk-..."
ADMIN_ROUTE = "manage" # Your secret admin URL parameter
ADMIN_PASSWORD_HASH = "..." # Generate this using the Hash Tool
"app.disable_admin" = "false" # Set to true to completely hide admin pages
```

### 2. Custom OpenAI Endpoints
If you are using a provider like **OpenRouter**, **Together.ai**, or a local **LM Studio** instance, you can set a custom base URL:
```toml
OPENAI_API_KEY = "your-provider-key"
OPENAI_BASE_URL = "https://openrouter.ai/api/v1"
"model.name" = "anthropic/claude-3-opus" # Use the provider's specific model string
```

### 3. Branding Overrides
You can override any branding element in secrets without redeploying:
```toml
"branding.logo_url" = "https://example.com/logo.png"
"branding.footer_text" = "Custom Footer"
"branding.hide_asklit_badge" = "false"
```

---

## 🏗️ Advanced: Local Development

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
