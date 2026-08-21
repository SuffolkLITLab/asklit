import streamlit as st
import uuid
import re
from asklit.auth import check_password
from asklit.config import get_setting
from asklit.rag import query_index
from asklit.prompts import build_messages, get_conversation_starters, get_prompt_configs
from asklit.llm import call_llm, estimate_tokens, get_allowed_models
from asklit.rate_limits import (
    check_conversation_turn_limit,
    check_prompt_length,
    check_rate_limits,
    increment_usage,
)
from asklit.db import get_connection
from asklit.ui import escape_html, safe_url

# st.set_page_config(
#     page_title=get_setting("app.title", "AskLit"),
#     page_icon="🤖",
#     layout="centered"
# )

CASUAL_PATTERNS = {
    "hi",
    "hi!",
    "hello",
    "hello!",
    "hey",
    "hey!",
    "thanks",
    "thank you",
    "ok",
    "okay",
    "yes",
    "no",
    "cool",
}


def should_search_knowledge_base(prompt):
    """Avoid paying the retrieval cost for short social turns."""
    normalized = prompt.strip().lower()
    if normalized in CASUAL_PATTERNS:
        return False
    return len(re.findall(r"\w+", normalized)) >= 3


def stream_response(response, placeholder):
    full_response = ""
    finish_reasons = []

    for chunk in response:
        if not hasattr(chunk, "choices") or not chunk.choices:
            continue

        choice = chunk.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason:
            finish_reasons.append(finish_reason)

        delta = getattr(choice, "delta", None)
        content = getattr(delta, "content", None) if delta else None
        if content is None and delta is not None:
            content = getattr(delta, "text", None)

        message = getattr(choice, "message", None)
        if content is None and message is not None:
            content = getattr(message, "content", None)

        if content:
            full_response += content
            placeholder.markdown(full_response + "▌")

    return full_response, finish_reasons


def render_waiting_indicator(placeholder):
    placeholder.markdown(
        """
        <style>
            .llm-waiting-indicator {
                align-items: center;
                color: inherit;
                display: inline-flex;
                font-style: italic;
                gap: 0.35rem;
                opacity: 0.78;
            }

            .llm-waiting-dots {
                display: inline-flex;
                gap: 0.12rem;
            }

            .llm-waiting-dot {
                animation: llm-waiting-pulse 1.2s ease-in-out infinite;
            }

            .llm-waiting-dot:nth-child(2) {
                animation-delay: 0.16s;
            }

            .llm-waiting-dot:nth-child(3) {
                animation-delay: 0.32s;
            }

            @keyframes llm-waiting-pulse {
                0%, 80%, 100% {
                    opacity: 0.25;
                    transform: translateY(0);
                }
                40% {
                    opacity: 1;
                    transform: translateY(-0.18rem);
                }
            }

            @media (prefers-reduced-motion: reduce) {
                .llm-waiting-dot {
                    animation: none;
                    opacity: 1;
                    transform: none;
                }
            }
        </style>
        <span class="llm-waiting-indicator" role="status" aria-live="polite">
            <span>Thinking</span>
            <span class="llm-waiting-dots" aria-hidden="true">
                <span class="llm-waiting-dot">.</span>
                <span class="llm-waiting-dot">.</span>
                <span class="llm-waiting-dot">.</span>
            </span>
        </span>
        """,
        unsafe_allow_html=True,
    )


def get_document_labels(document_ids):
    if not document_ids:
        return {}

    document_ids = set(document_ids)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, filename FROM documents")
    labels = {
        row["id"]: row["filename"]
        for row in cursor.fetchall()
        if row["id"] in document_ids
    }
    conn.close()
    return labels


def render_citations(context_chunks):
    document_ids = {
        chunk["metadata"].get("document_id")
        for chunk in context_chunks
        if chunk.get("metadata", {}).get("document_id")
    }
    document_labels = get_document_labels(document_ids)

    with st.expander("Sources"):
        for i, chunk in enumerate(context_chunks):
            metadata = chunk.get("metadata", {})
            document_id = metadata.get("document_id")
            filename = document_labels.get(document_id, "Knowledge base document")
            page = metadata.get("page_number", "N/A")

            st.write(f"**Source {i + 1}:** {filename}, page {page}")
            st.write(chunk["content"])
            st.divider()


def has_user_messages(messages):
    return any(message.get("role") == "user" for message in messages)


def render_prompt_selector(prompt_configs):
    if len(prompt_configs) <= 1:
        return prompt_configs[0]

    keys = [config["key"] for config in prompt_configs]
    current_key = st.session_state.get("active_prompt_key", keys[0])
    if current_key not in keys:
        current_key = keys[0]

    labels = {config["key"]: config["label"] for config in prompt_configs}
    selected_key = st.sidebar.radio(
        "AskLit",
        keys,
        format_func=lambda key: labels.get(key, key),
        index=keys.index(current_key),
    )
    if selected_key != current_key:
        st.session_state.active_prompt_key = selected_key
        st.session_state.messages = []
        st.session_state.conversation_id = str(uuid.uuid4())
        st.rerun()

    st.session_state.active_prompt_key = selected_key
    for config in prompt_configs:
        if config["key"] == selected_key:
            return config
    return prompt_configs[0]


def render_model_selector():
    configured_model = str(get_setting("model.name", "gpt-5.4-mini"))
    selection_enabled = (
        str(get_setting("model.allow_user_selection", "false")).lower() == "true"
    )
    allowed_models = get_allowed_models()
    if not selection_enabled or not allowed_models:
        return configured_model

    current_model = st.session_state.get("active_model", configured_model)
    if current_model not in allowed_models:
        current_model = (
            configured_model
            if configured_model in allowed_models
            else allowed_models[0]
        )
    selected_model = st.sidebar.selectbox(
        "Model",
        allowed_models,
        index=allowed_models.index(current_model),
        help="Choose a model for this conversation. Azure gateway limits still apply.",
    )
    st.session_state.active_model = selected_model
    return selected_model


def render_conversation_starters(prompt_key=None):
    starters = get_conversation_starters(prompt_key)
    if not starters:
        return None

    columns = st.columns(min(len(starters), 3))
    for index, starter in enumerate(starters):
        with columns[index % len(columns)]:
            if st.button(
                starter["label"],
                key=f"conversation_starter_{index}",
                use_container_width=True,
            ):
                return starter["prompt"]

    return None


def main():
    if not check_password():
        st.stop()

    # Branding: Logo in sidebar or top
    logo_url = get_setting("branding.logo_url")
    homepage_url = get_setting("branding.homepage_url")
    logo_width = int(get_setting("branding.logo_width", 200))
    logo_url = safe_url(logo_url)
    homepage_url = safe_url(homepage_url)
    if logo_url:
        if homepage_url:
            st.sidebar.markdown(
                f'<a href="{escape_html(homepage_url)}" target="_blank" rel="noopener noreferrer">'
                f'<img src="{escape_html(logo_url)}" width="{logo_width}"></a>',
                unsafe_allow_html=True,
            )
        else:
            st.sidebar.image(logo_url, width=logo_width)

    st.sidebar.divider()
    prompt_configs = get_prompt_configs()
    active_prompt_config = render_prompt_selector(prompt_configs)
    active_prompt_key = active_prompt_config["key"]
    active_model = render_model_selector()

    st.title(get_setting("app.title", "AskLit"))

    if "messages" not in st.session_state:
        st.session_state.messages = []
        welcome = get_setting(
            "app.welcome_message", "Welcome! How can I help you today?"
        )
        st.session_state.messages.append({"role": "assistant", "content": welcome})

    if "conversation_id" not in st.session_state:
        st.session_state.conversation_id = str(uuid.uuid4())

    # Display chat history
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    turn_allowed, turn_msg = check_conversation_turn_limit(st.session_state.messages)
    if not turn_allowed:
        st.info(turn_msg)

    starter_prompt = None
    if turn_allowed and not has_user_messages(st.session_state.messages):
        starter_prompt = render_conversation_starters(active_prompt_key)

    # Chat input
    chat_prompt = st.chat_input("What is your question?", disabled=not turn_allowed)
    if prompt := starter_prompt or chat_prompt:
        # Check rate limits
        allowed, msg = check_rate_limits(st.session_state.conversation_id)
        if not allowed:
            st.error(msg)
            st.stop()

        allowed, msg = check_prompt_length(prompt)
        if not allowed:
            st.error(msg)
            st.stop()

        allowed, msg = check_conversation_turn_limit(st.session_state.messages)
        if not allowed:
            st.error(msg)
            st.stop()

        # Display user message
        with st.chat_message("user"):
            st.markdown(prompt)
        st.session_state.messages.append({"role": "user", "content": prompt})

        # RAG: Retrieve context (only if documents exist)
        context_chunks = []
        try:
            conn = get_connection()
            cursor = conn.cursor()
            connected_files = active_prompt_config.get("connected_files") or []
            if connected_files:
                placeholders = ",".join("?" for _ in connected_files)
                cursor.execute(
                    f"""
                    SELECT count(*) as count
                    FROM documents
                    WHERE status = 'indexed'
                    AND knowledgebase = ?
                    AND filename IN ({placeholders})
                    """,
                    [active_prompt_config["knowledgebase"], *connected_files],
                )
            else:
                cursor.execute(
                    """
                    SELECT count(*) as count
                    FROM documents
                    WHERE status = 'indexed' AND knowledgebase = ?
                    """,
                    (active_prompt_config["knowledgebase"],),
                )
            doc_count = cursor.fetchone()["count"]
            conn.close()

            if doc_count > 0 and should_search_knowledge_base(prompt):
                with st.status("Searching knowledge base...", expanded=False) as status:
                    from asklit.rag import get_collection

                    collection = get_collection()
                    if collection.count() > 0:
                        context_chunks = query_index(
                            prompt,
                            knowledgebase=active_prompt_config["knowledgebase"],
                            connected_files=connected_files,
                        )
                    status.update(
                        label="Search complete!", state="complete", expanded=False
                    )
        except Exception as e:
            # Show error if in admin mode, otherwise ignore
            if st.session_state.get("is_admin_authenticated"):
                st.error(f"DEBUG: Knowledge base search failed: {str(e)}")

        # Build messages
        messages = build_messages(
            prompt,
            context_chunks,
            st.session_state.messages[:-1],
            prompt_key=active_prompt_key,
        )

        # Call LLM
        with st.chat_message("assistant"):
            if st.session_state.get("is_admin_authenticated"):
                st.caption(
                    f"Using model: {active_model} via {get_setting('model.provider')}"
                )

            response_placeholder = st.empty()
            full_response = ""

            try:
                render_waiting_indicator(response_placeholder)
                response = call_llm(messages, model_override=active_model)
                full_response, finish_reasons = stream_response(
                    response, response_placeholder
                )

                if not full_response and "length" in finish_reasons:
                    retry_tokens = max(
                        int(get_setting("model.max_tokens", 1000)) * 2, 2000
                    )
                    response = call_llm(
                        messages,
                        max_tokens_override=retry_tokens,
                        model_override=active_model,
                    )
                    full_response, finish_reasons = stream_response(
                        response, response_placeholder
                    )

                if not full_response:
                    full_response = "The model returned an empty response. This can happen if the model name is incorrect or if the context is too large."

                response_placeholder.markdown(full_response)

                # Show citations if enabled
                if (
                    get_setting("retrieval.show_citations", "true") == "true"
                    and context_chunks
                ):
                    render_citations(context_chunks)

            except Exception as e:
                if st.session_state.get("is_admin_authenticated"):
                    st.error(f"Error calling LLM: {str(e)}")
                else:
                    st.error(
                        "The language model is temporarily unavailable or this app has reached its usage limit. Please try again later."
                    )
                full_response = (
                    "I'm sorry, I encountered an error. Please try again later."
                )

            st.session_state.messages.append(
                {"role": "assistant", "content": full_response}
            )
            increment_usage(
                st.session_state.conversation_id,
                estimate_tokens(prompt) + estimate_tokens(full_response),
            )

            # Log to DB
            if get_setting("logging.enabled", "true") == "true":
                try:
                    conn = get_connection()
                    cursor = conn.cursor()
                    # Ensure conversation exists
                    cursor.execute(
                        "INSERT OR IGNORE INTO conversations (id, title) VALUES (?, ?)",
                        (st.session_state.conversation_id, prompt[:50]),
                    )
                    # Save messages
                    cursor.execute(
                        "INSERT INTO messages (conversation_id, role, content, tokens) VALUES (?, ?, ?, ?)",
                        (
                            st.session_state.conversation_id,
                            "user",
                            prompt,
                            estimate_tokens(prompt),
                        ),
                    )
                    cursor.execute(
                        "INSERT INTO messages (conversation_id, role, content, tokens) VALUES (?, ?, ?, ?)",
                        (
                            st.session_state.conversation_id,
                            "assistant",
                            full_response,
                            estimate_tokens(full_response),
                        ),
                    )
                    conn.commit()
                    conn.close()
                except Exception as e:
                    st.session_state["last_logging_error"] = str(e)

    # Global Footer
    st.divider()
    supp_text = get_setting("branding.supplemental_footer_text", "")
    hide_badge = (
        str(get_setting("branding.hide_asklit_badge", "false")).lower() == "true"
    )

    footer_html = '<div style="text-align: center; opacity: 0.7; font-size: 0.8rem;">'
    if supp_text:
        footer_html += f"<span>{escape_html(supp_text)}</span>"
        if not hide_badge:
            footer_html += " | "

    if not hide_badge:
        footer_html += '<a href="https://suffolklitlab.org/asklit" target="_blank" rel="noopener noreferrer">Made with AskLit</a>'

    footer_html += "</div>"
    st.markdown(footer_html, unsafe_allow_html=True)


if __name__ == "__main__":
    main()
