"""Step 3: a conversational preview that behaves like the exported app."""

import streamlit as st

from asklit.config import get_setting
from asklit.experiments import build_experiment_messages, response_text
from asklit.llm import call_llm
from asklit.observability import safe_error_message
from asklit.rag import query_index
from asklit.scaffold.config import DEFAULT_MODEL_NAME, normalize_prompt_profiles
from asklit.scaffold.endpoints import get_endpoint_model_choices, host_runnable_models
from asklit.scaffold.knowledge import get_document_labels
from asklit.scaffold.ui import session_paths

DEFAULT_PREVIEW_CHAT_TURNS = 12


def preview_chat_turn_limit():
    """Cap preview questions so one session cannot drain a shared class budget."""
    try:
        limit = int(
            get_setting(
                "limits.max_preview_chat_turns", DEFAULT_PREVIEW_CHAT_TURNS
            )
        )
    except (TypeError, ValueError):
        limit = DEFAULT_PREVIEW_CHAT_TURNS
    return max(limit, 1)


def preview_model_choices(model_config):
    """Offer only models this scaffolder host is actually allowed to run.

    The exported app's approved-model list is a deployment setting for someone
    else's key, so it deliberately does not widen what the preview may call.
    """
    provider = model_config.get("provider", "openai")
    try:
        model_choices, _source, _discovery = get_endpoint_model_choices(provider, [])
    except Exception:
        model_choices = []
    choices = host_runnable_models(model_choices)
    configured = str(model_config.get("name") or "").strip()
    if configured and configured not in choices:
        choices.insert(0, configured)
    return choices or [DEFAULT_MODEL_NAME]


def _render_chat_message(message):
    """Render one transcript entry with its model tag and retrieved passages."""
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message.get("model"):
            st.caption(f"Answered by {message['model']}")
        sources = message.get("sources")
        if sources:
            with st.expander(f"Sources used ({len(sources)})", expanded=False):
                for source in sources:
                    page = f" · page {source['page']}" if source.get("page") else ""
                    st.markdown(f"**{source['filename']}**{page}")
                    st.caption(source["content"])
        elif message["role"] == "assistant" and message.get("retrieval_ran"):
            st.caption(
                "No passages were retrieved, so this answer came from the model's "
                "general knowledge rather than your knowledge base."
            )


def render_chat_step():
    """Render a conversational preview that behaves like the exported app."""
    st.header("Try the advisor")
    st.write(
        "Ask a few questions to see how your prompt and knowledge base work together. "
        "This chat is a session-only preview; use the Evaluate step for repeatable tests."
    )
    profiles = normalize_prompt_profiles(
        st.session_state.app_config.get("prompt_profiles")
    )
    st.session_state.app_config["prompt_profiles"] = profiles
    profile_keys = [profile["key"] for profile in profiles]
    labels = {profile["key"]: profile["label"] for profile in profiles}
    model_config = st.session_state.app_config.get("model", {})
    model_choices = preview_model_choices(model_config)

    control_columns = st.columns(2)
    with control_columns[0]:
        selected_key = st.selectbox(
            "Prompt to try",
            profile_keys,
            format_func=lambda key: labels[key],
            key="preview_chat_prompt",
            help="Switch prompts without leaving the interactive preview.",
        )
    with control_columns[1]:
        configured_model = str(model_config.get("name") or model_choices[0])
        selected_model = st.selectbox(
            "Model to try",
            model_choices,
            index=(
                model_choices.index(configured_model)
                if configured_model in model_choices
                else 0
            ),
            key="preview_chat_model",
            help=(
                "Switching keeps the transcript, so you can ask the same question "
                "of two models and compare."
            ),
        )

    profile = next(profile for profile in profiles if profile["key"] == selected_key)
    st.caption(
        f"Knowledge base: **{profile['knowledgebase']}** · Model: **{selected_model}**"
    )

    if "preview_chat_messages" not in st.session_state:
        st.session_state.preview_chat_messages = []
    messages = st.session_state.preview_chat_messages
    for message in messages:
        _render_chat_message(message)

    turn_limit = preview_chat_turn_limit()
    asked = sum(1 for message in messages if message.get("role") == "user")
    remaining = turn_limit - asked

    if st.button("Clear preview chat"):
        st.session_state.preview_chat_messages = []
        st.rerun()

    if remaining <= 0:
        st.warning(
            f"This preview allows {turn_limit} questions per conversation so a class "
            "shares the model budget fairly. Clear the preview chat to start a new "
            "one, or move to **4. Evaluate** for repeatable tests that measure "
            "several prompts and models at once."
        )
        return
    st.caption(f"{remaining} of {turn_limit} preview questions remaining.")

    starter_prompt = None
    if not asked and profile.get("conversation_starters"):
        st.caption("Try a conversation starter")
        starters = profile["conversation_starters"]
        starter_columns = st.columns(min(len(starters), 3))
        for index, starter in enumerate(starters):
            with starter_columns[index % len(starter_columns)]:
                if st.button(
                    starter,
                    key=f"preview_starter_{selected_key}_{index}",
                    use_container_width=True,
                ):
                    starter_prompt = starter

    question = starter_prompt or st.chat_input("Ask the advisor a question")
    if not question:
        return

    history = [
        {"role": message["role"], "content": message["content"]} for message in messages
    ]
    messages.append({"role": "user", "content": question})

    db_path, chroma_path = session_paths()
    context_chunks = []
    try:
        context_chunks = query_index(
            question,
            n_results=5,
            knowledgebase=profile["knowledgebase"],
            connected_files=profile.get("connected_files"),
            db_path=db_path,
            chroma_path=chroma_path,
        )
        with st.spinner("Thinking…"):
            response = call_llm(
                # The exported chat sends prior turns, so the preview must too.
                build_experiment_messages(
                    profile["prompt"], question, context_chunks, chat_history=history
                ),
                stream=False,
                model_override=selected_model,
                provider_override=model_config.get("provider"),
                extra_allowed_models=model_choices,
            )
        answer = response_text(response) or "The model returned an empty response."
    except Exception as exc:
        answer = f"The preview could not answer this question: {safe_error_message(exc)}"

    document_labels = get_document_labels(db_path)
    messages.append(
        {
            "role": "assistant",
            "content": answer,
            "model": selected_model,
            "retrieval_ran": True,
            "sources": [
                {
                    "filename": document_labels.get(
                        chunk.get("metadata", {}).get("document_id"),
                        "Knowledge base document",
                    ),
                    "page": chunk.get("metadata", {}).get("page_number"),
                    "content": chunk.get("content", "")[:1200],
                }
                for chunk in context_chunks
            ],
        }
    )
    st.rerun()
