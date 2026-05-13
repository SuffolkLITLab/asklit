import os
import json
import yaml
from asklit.db import get_connection
from asklit.config import get_setting, set_setting

DEFAULT_PROMPT_PATHS = [
    os.path.join("prompts", "default_system_prompt.yml"),
    os.path.join("prompts", "default_system_prompt.yaml"),
    os.path.join("prompts", "default_system_prompt.md"),
]


def normalize_conversation_starters(value):
    if not value:
        return []

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        try:
            parsed = json.loads(value)
            return normalize_conversation_starters(parsed)
        except json.JSONDecodeError:
            return [line.strip() for line in value.splitlines() if line.strip()]

    if not isinstance(value, list):
        return []

    starters = []
    for starter in value:
        if isinstance(starter, str):
            text = starter.strip()
            if text:
                starters.append({"label": text, "prompt": text})
        elif isinstance(starter, dict):
            prompt = str(starter.get("prompt", "")).strip()
            label = str(starter.get("label") or starter.get("title") or prompt).strip()
            if prompt:
                starters.append({"label": label, "prompt": prompt})

    return starters


def load_default_prompt_config():
    for path in DEFAULT_PROMPT_PATHS:
        if not os.path.exists(path):
            continue

        with open(path, "r") as f:
            content = f.read()

        if path.endswith((".yml", ".yaml")):
            data = yaml.safe_load(content) or {}
            if isinstance(data, dict):
                return {
                    "prompt": data.get("prompt", "You are a helpful assistant."),
                    "conversation_starters": normalize_conversation_starters(
                        data.get("conversation starters") or data.get("conversation_starters")
                    ),
                }
            if isinstance(data, str):
                return {"prompt": data, "conversation_starters": []}

        return {"prompt": content, "conversation_starters": []}

    return {"prompt": "You are a helpful assistant.", "conversation_starters": []}


def get_active_prompt():
    """Retrieve the active system prompt from SQLite or the default file."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT content FROM prompt_versions WHERE is_active = 1 ORDER BY created_at DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return row['content']

    return load_default_prompt_config()["prompt"]


def get_conversation_starters():
    missing_value = "__ASKLIT_CONVERSATION_STARTERS_MISSING__"
    configured_starters = get_setting("app.conversation_starters", missing_value)
    if configured_starters != missing_value:
        return normalize_conversation_starters(configured_starters)

    return load_default_prompt_config()["conversation_starters"]


def save_conversation_starters(starters):
    normalized_starters = normalize_conversation_starters(starters)
    set_setting("app.conversation_starters", json.dumps(normalized_starters))

def build_messages(user_query, context_chunks, chat_history=None):
    """Construct the messages list for the LLM call."""
    system_prompt = get_active_prompt()
    
    # Add context to system prompt or as a separate message
    # Limit total context to avoid hitting token limits
    context_parts = []
    current_length = 0
    max_context_chars = 8000 # Safety limit
    
    for i, c in enumerate(context_chunks):
        content = c['content'].strip()
        if len(content) < 80:
            continue
        if current_length + len(content) > max_context_chars:
            break
        context_parts.append(f"--- SOURCE {i+1} ---\n{content}")
        current_length += len(content)
    
    context_str = "\n\n".join(context_parts)
    
    full_system_content = f"{system_prompt}\n\nRELEVANT CONTEXT FROM THE KNOWLEDGE BASE:\n{context_str}\n\nINSTRUCTIONS FOR USING CONTEXT:\n1. When context is provided and it is relevant, ground the answer in that context before adding general background.\n2. If the context only partially answers the question, say what the context supports and then add any clearly labeled general guidance.\n3. If the context does not contain the answer, or if the user is asking a general question, use your general knowledge to provide a helpful response."
    
    messages = [{"role": "system", "content": full_system_content}]
    
    # Filter chat history to keep only role and content
    if chat_history:
        for msg in chat_history:
            if msg["role"] in ["user", "assistant"]:
                messages.append({"role": msg["role"], "content": msg["content"]})
        
    messages.append({"role": "user", "content": user_query})
    
    return messages

def save_new_prompt(content, db_path=None):
    """Save a new prompt version and set it as active."""
    conn = get_connection(db_path=db_path)
    cursor = conn.cursor()
    # Deactivate current
    cursor.execute("UPDATE prompt_versions SET is_active = 0")
    # Insert new
    cursor.execute("INSERT INTO prompt_versions (content, is_active) VALUES (?, 1)", (content,))
    conn.commit()
    conn.close()
