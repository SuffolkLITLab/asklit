import streamlit as st
from asklit.config import get_setting, set_setting
from asklit.prompts import (
    get_active_prompt,
    get_conversation_starters,
    get_prompt_configs,
    save_prompt_metadata,
    save_conversation_starters,
    save_new_prompt,
)

# st.set_page_config(page_title="Admin Panel", page_icon="⚙️")


def option_index(options, value, default=0):
    return options.index(value) if value in options else default


def main():
    st.title("⚙️ Admin Panel")

    if not st.session_state.get("is_admin_authenticated"):
        st.error("Access denied. Please login.")
        st.stop()

    tab1, tab2, tab3 = st.tabs(
        ["General Settings", "Model & RAG", "Prompt Engineering"]
    )

    with tab1:
        st.header("General Settings")

        site_title = st.text_input(
            "Site Title", value=get_setting("app.title", "AskLit")
        )
        welcome_msg = st.text_area(
            "Welcome Message",
            value=get_setting(
                "app.welcome_message", "Welcome! How can I help you today?"
            ),
        )

        access_mode = st.selectbox(
            "Access Mode",
            ["public", "password"],
            index=0 if get_setting("app.access_mode") == "public" else 1,
        )

        daily_limit = st.number_input(
            "Daily Request Limit",
            min_value=0,
            value=int(get_setting("limits.daily_request_limit", 100)),
            help="Per browser session. Set to 0 to disable this guard.",
        )

        max_turns = st.number_input(
            "Max Conversation Turns",
            min_value=0,
            value=int(get_setting("limits.max_conversation_turns", 10)),
            help="Maximum user messages in one conversation. Set to 0 for unlimited.",
        )

        max_prompt_length = st.number_input(
            "Max Prompt Length",
            min_value=0,
            value=int(get_setting("limits.max_prompt_length", 2000)),
            help="Maximum characters in a single user message. Set to 0 for unlimited.",
        )

        daily_token_limit = st.number_input(
            "Daily Token Limit",
            min_value=0,
            value=int(get_setting("limits.daily_token_limit", 50000)),
            help="Approximate per-session token budget. Set to 0 for unlimited.",
        )

        logging_enabled = st.checkbox(
            "Enable Usage Logging",
            value=(get_setting("logging.enabled", "true") == "true"),
        )

        if st.button("Save General Settings"):
            set_setting("app.title", site_title)
            set_setting("app.welcome_message", welcome_msg)
            set_setting("app.access_mode", access_mode)
            set_setting("limits.daily_request_limit", daily_limit)
            set_setting("limits.max_conversation_turns", max_turns)
            set_setting("limits.max_prompt_length", max_prompt_length)
            set_setting("limits.daily_token_limit", daily_token_limit)
            set_setting("logging.enabled", "true" if logging_enabled else "false")
            st.success("Settings saved!")

    with tab2:
        st.header("Model & RAG Settings")

        provider_options = [
            "openai",
            "azure",
            "azure_apim",
            "anthropic",
            "google",
            "ollama",
        ]
        provider = st.selectbox(
            "LLM Provider",
            provider_options,
            index=option_index(
                provider_options, get_setting("model.provider", "openai")
            ),
            help="Use 'azure_apim' for a protected Azure AI gateway, 'openai' for OpenAI-compatible proxies, or 'azure' for direct Azure OpenAI endpoints.",
        )

        model_name = st.text_input(
            "Model Name / Deployment ID",
            value=get_setting("model.name", "gpt-5.4-mini"),
            help="The name of the model or the Azure Deployment ID.",
        )

        temp = st.slider(
            "Temperature", 0.0, 1.0, float(get_setting("model.temperature", 1.0)), 0.1
        )

        disable_temp = st.checkbox(
            "Force Disable Temperature",
            value=(get_setting("model.disable_temperature", "false") == "true"),
            help="Some models (o1, o3, gpt-5) do not support the temperature parameter. This is automatically handled for those families, but you can force it off here.",
        )

        local_embed_model = st.text_input(
            "Local Embedding Model",
            value=get_setting("model.local_embedding_model", "all-MiniLM-L6-v2"),
            help="This model runs locally on your CPU/GPU.",
        )

        remote_embed_model = st.text_input(
            "Remote Embedding Model",
            value=get_setting("model.embedding_model", "text-embedding-3-small"),
            help="Used when 'Use Local Embeddings' is disabled. Uses your LLM provider API.",
        )

        embedding_provider_options = [
            "openai",
            "azure",
            "azure_apim",
            "anthropic",
            "google",
            "ollama",
        ]
        embedding_provider = st.selectbox(
            "Embedding Provider",
            embedding_provider_options,
            index=option_index(
                embedding_provider_options,
                get_setting(
                    "model.embedding_provider", get_setting("model.provider", "openai")
                ),
            ),
            help="Provider used for remote embeddings. Usually this matches the LLM provider.",
        )

        use_local = st.checkbox(
            "Use Local Embeddings",
            value=(get_setting("model.use_local_embeddings", "true") == "true"),
            help="Enable to use a local model (free, no API key). Disable to use the remote model.",
        )

        max_tokens = st.number_input(
            "Max Output Tokens", value=int(get_setting("model.max_tokens", 1000))
        )

        hard_max_tokens = st.number_input(
            "Hard Output Token Ceiling",
            min_value=1,
            value=int(get_setting("limits.max_output_tokens_hard", 4000)),
            help="Server-side ceiling applied even when a retry requests more output.",
        )

        reasoning_effort = st.selectbox(
            "Reasoning Effort",
            ["minimal", "low", "medium", "high"],
            index=option_index(
                ["minimal", "low", "medium", "high"],
                get_setting("model.reasoning_effort", "low"),
                default=1,
            ),
            help="Used by reasoning models such as GPT-5. Lower values usually respond faster and leave more budget for visible output.",
        )

        allow_user_selection = st.checkbox(
            "Let users choose an approved model",
            value=(
                str(get_setting("model.allow_user_selection", "false")).lower()
                == "true"
            ),
            help="Shows a model selector in the chat sidebar. Requests remain limited to the allowlist below.",
        )
        allowed_models = st.text_area(
            "Approved Models",
            value=str(get_setting("model.allowed_models", "")),
            help="Comma-separated deployment names. The Azure gateway must use the same allowlist.",
        )

        top_k = st.number_input(
            "Retrieval Top-K", value=int(get_setting("retrieval.top_k", 5))
        )

        show_citations = st.checkbox(
            "Show Citations",
            value=(get_setting("retrieval.show_citations", "true") == "true"),
        )

        if st.button("Save Model Settings"):
            set_setting("model.provider", provider)
            set_setting("model.name", model_name)
            set_setting("model.temperature", temp)
            set_setting(
                "model.disable_temperature", "true" if disable_temp else "false"
            )
            set_setting("model.local_embedding_model", local_embed_model)
            set_setting("model.embedding_provider", embedding_provider)
            set_setting("model.embedding_model", remote_embed_model)
            set_setting("model.use_local_embeddings", "true" if use_local else "false")
            set_setting("model.max_tokens", max_tokens)
            set_setting("limits.max_output_tokens_hard", hard_max_tokens)
            set_setting("model.reasoning_effort", reasoning_effort)
            set_setting(
                "model.allow_user_selection",
                "true" if allow_user_selection else "false",
            )
            set_setting("model.allowed_models", allowed_models)
            set_setting("retrieval.top_k", top_k)
            set_setting(
                "retrieval.show_citations", "true" if show_citations else "false"
            )
            st.success("Model settings saved!")

    with tab3:
        st.header("Prompt Engineering")

        prompt_configs = get_prompt_configs()
        prompt_keys = [config["key"] for config in prompt_configs]
        selected_key = st.selectbox(
            "Prompt / Knowledge Base Pairing",
            prompt_keys,
            format_func=lambda key: next(
                config["label"] for config in prompt_configs if config["key"] == key
            ),
        )
        selected_config = next(
            config for config in prompt_configs if config["key"] == selected_key
        )
        st.caption(
            f"Knowledge base: {selected_config['knowledgebase']}"
            + (
                f" | Connected files: {', '.join(selected_config['connected_files'])}"
                if selected_config["connected_files"]
                else ""
            )
        )
        knowledgebase = st.text_input(
            "Knowledge Base Name",
            value=selected_config["knowledgebase"],
        )
        connected_files = st.text_area(
            "Connected Files",
            value="\n".join(selected_config["connected_files"]),
            height=120,
            help="Optional. Leave blank to connect all indexed files in this knowledge base.",
        )

        current_prompt = get_active_prompt(selected_key)
        new_prompt = st.text_area(
            "Active System Prompt", value=current_prompt, height=400
        )

        current_starters = "\n".join(
            starter["prompt"] for starter in get_conversation_starters(selected_key)
        )
        new_starters = st.text_area(
            "Conversation Starters",
            value=current_starters,
            height=160,
            help="One starter prompt per line. These are shown as cards before the user starts a chat.",
        )

        if st.button("Update Prompt Settings"):
            save_new_prompt(new_prompt, prompt_key=selected_key)
            save_conversation_starters(new_starters, prompt_key=selected_key)
            save_prompt_metadata(
                knowledgebase,
                connected_files,
                prompt_key=selected_key,
            )
            st.success("Prompt settings updated!")


if __name__ == "__main__":
    main()
