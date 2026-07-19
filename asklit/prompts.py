import os
import json
import glob
import re
import yaml
from asklit.db import get_connection
from asklit.config import get_setting, set_setting

PROMPTS_DIR = "prompts"
DEFAULT_PROMPT_KEY = "default"
DEFAULT_KNOWLEDGEBASE = "default"
DEFAULT_PROMPT_PATHS = [
    os.path.join(PROMPTS_DIR, "default_system_prompt.yml"),
    os.path.join(PROMPTS_DIR, "default_system_prompt.yaml"),
    os.path.join(PROMPTS_DIR, "default_system_prompt.md"),
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


def slugify_prompt_key(value):
    value = os.path.splitext(os.path.basename(value or ""))[0]
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    if value in {"", "default-system-prompt"}:
        return DEFAULT_PROMPT_KEY
    return value


def prompt_label_from_key(prompt_key):
    if prompt_key == DEFAULT_PROMPT_KEY:
        return "Default"
    return prompt_key.replace("-", " ").replace("_", " ").title()


def normalize_file_list(value):
    if not value:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return normalize_file_list(parsed)
        except json.JSONDecodeError:
            pass
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def normalize_knowledgebase_config(data, prompt_key):
    knowledgebase = data.get("knowledgebase") or data.get("knowledge_base")
    files = (
        data.get("connected_files")
        or data.get("connected files")
        or data.get("files")
        or []
    )

    if isinstance(knowledgebase, dict):
        name = (
            knowledgebase.get("name")
            or knowledgebase.get("key")
            or knowledgebase.get("id")
            or prompt_key
        )
        files = (
            knowledgebase.get("connected_files")
            or knowledgebase.get("connected files")
            or knowledgebase.get("files")
            or files
        )
    elif knowledgebase:
        name = str(knowledgebase)
    else:
        name = DEFAULT_KNOWLEDGEBASE if prompt_key == DEFAULT_PROMPT_KEY else prompt_key

    name = str(name).strip() or DEFAULT_KNOWLEDGEBASE
    return name, normalize_file_list(files)


def load_prompt_config_from_path(path):
    prompt_key = slugify_prompt_key(path)
    with open(path, "r") as f:
        content = f.read()

    config = {
        "key": prompt_key,
        "label": prompt_label_from_key(prompt_key),
        "prompt": content,
        "conversation_starters": [],
        "knowledgebase": DEFAULT_KNOWLEDGEBASE,
        "connected_files": [],
        "path": path,
    }

    if path.endswith((".yml", ".yaml")):
        data = yaml.safe_load(content) or {}
        if isinstance(data, dict):
            prompt = data.get("prompt", "You are a helpful assistant.")
            knowledgebase, connected_files = normalize_knowledgebase_config(
                data, prompt_key
            )
            config.update(
                {
                    "label": str(
                        data.get("label")
                        or data.get("title")
                        or data.get("name")
                        or config["label"]
                    ),
                    "prompt": prompt,
                    "conversation_starters": normalize_conversation_starters(
                        data.get("conversation starters")
                        or data.get("conversation_starters")
                    ),
                    "knowledgebase": knowledgebase,
                    "connected_files": connected_files,
                }
            )
        elif isinstance(data, str):
            config["prompt"] = data

    return config


def discover_prompt_paths():
    paths = []
    if os.path.isdir(PROMPTS_DIR):
        for pattern in ("*.yml", "*.yaml", "*.md"):
            paths.extend(glob.glob(os.path.join(PROMPTS_DIR, pattern)))

    if not paths:
        paths = [path for path in DEFAULT_PROMPT_PATHS if os.path.exists(path)]

    paths = sorted(set(paths))
    return sorted(paths, key=lambda path: (path not in DEFAULT_PROMPT_PATHS, path))


def load_prompt_configs():
    configs = []
    seen_keys = set()

    for path in discover_prompt_paths():
        if not os.path.exists(path):
            continue
        config = load_prompt_config_from_path(path)
        base_key = config["key"]
        suffix = 2
        while config["key"] in seen_keys:
            config["key"] = f"{base_key}-{suffix}"
            suffix += 1
        seen_keys.add(config["key"])
        configs.append(config)

    if configs:
        return configs

    return [
        {
            "key": DEFAULT_PROMPT_KEY,
            "label": "Default",
            "prompt": "You are a helpful assistant.",
            "conversation_starters": [],
            "knowledgebase": DEFAULT_KNOWLEDGEBASE,
            "connected_files": [],
            "path": None,
        }
    ]


def load_default_prompt_config():
    return load_prompt_configs()[0]


def get_prompt_config(prompt_key=None):
    configs = load_prompt_configs()
    prompt_key = prompt_key or configs[0]["key"]
    config = configs[0]
    for candidate in configs:
        if candidate["key"] == prompt_key:
            config = candidate
            break

    missing_value = "__ASKLIT_PROMPT_METADATA_MISSING__"
    configured_knowledgebase = get_setting(
        f"prompts.{config['key']}.knowledgebase", missing_value
    )
    configured_files = get_setting(
        f"prompts.{config['key']}.connected_files", missing_value
    )
    if configured_knowledgebase != missing_value:
        config = {**config, "knowledgebase": configured_knowledgebase}
    if configured_files != missing_value:
        config = {**config, "connected_files": normalize_file_list(configured_files)}
    return config


def get_prompt_configs():
    return [get_prompt_config(config["key"]) for config in load_prompt_configs()]


def get_active_prompt(prompt_key=None):
    """Retrieve the active system prompt from SQLite or the default file."""
    config = get_prompt_config(prompt_key)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT content
        FROM prompt_versions
        WHERE is_active = 1 AND prompt_key = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (config["key"],),
    )
    row = cursor.fetchone()
    conn.close()

    if row:
        return row["content"]

    return config["prompt"]


def get_conversation_starters(prompt_key=None):
    prompt_key = get_prompt_config(prompt_key)["key"]
    missing_value = "__ASKLIT_CONVERSATION_STARTERS_MISSING__"
    configured_starters = get_setting(
        f"prompts.{prompt_key}.conversation_starters", missing_value
    )
    if configured_starters == missing_value and prompt_key == DEFAULT_PROMPT_KEY:
        configured_starters = get_setting("app.conversation_starters", missing_value)
    if configured_starters != missing_value:
        return normalize_conversation_starters(configured_starters)

    return get_prompt_config(prompt_key)["conversation_starters"]


def save_conversation_starters(starters, prompt_key=None):
    prompt_key = get_prompt_config(prompt_key)["key"]
    normalized_starters = normalize_conversation_starters(starters)
    set_setting(
        f"prompts.{prompt_key}.conversation_starters",
        json.dumps(normalized_starters),
    )


def save_prompt_metadata(knowledgebase, connected_files, prompt_key=None):
    config = get_prompt_config(prompt_key)
    prompt_key = config["key"]
    knowledgebase = str(knowledgebase or "").strip() or config["knowledgebase"]
    set_setting(f"prompts.{prompt_key}.knowledgebase", knowledgebase)
    set_setting(
        f"prompts.{prompt_key}.connected_files",
        json.dumps(normalize_file_list(connected_files)),
    )


def build_messages(user_query, context_chunks, chat_history=None, prompt_key=None):
    """Construct the messages list for the LLM call."""
    system_prompt = get_active_prompt(prompt_key)

    # Add context to system prompt or as a separate message
    # Limit total context to avoid hitting token limits
    context_parts = []
    current_length = 0
    max_context_chars = 8000  # Safety limit

    for i, c in enumerate(context_chunks):
        content = c["content"].strip()
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


def save_new_prompt(content, db_path=None, prompt_key=None):
    """Save a new prompt version and set it as active."""
    prompt_key = get_prompt_config(prompt_key)["key"]
    conn = get_connection(db_path=db_path)
    cursor = conn.cursor()
    # Deactivate current
    cursor.execute(
        "UPDATE prompt_versions SET is_active = 0 WHERE prompt_key = ?",
        (prompt_key,),
    )
    # Insert new
    cursor.execute(
        "INSERT INTO prompt_versions (prompt_key, content, is_active) VALUES (?, ?, 1)",
        (prompt_key, content),
    )
    conn.commit()
    conn.close()
