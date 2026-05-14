import streamlit as st
from datetime import datetime, timedelta
from asklit.db import get_connection
from asklit.config import get_setting


def get_int_setting(key, default):
    try:
        return int(get_setting(key, default))
    except (TypeError, ValueError):
        return default


def count_user_turns(messages):
    return sum(1 for message in messages if message.get("role") == "user")


def check_conversation_turn_limit(messages):
    max_turns = get_int_setting("limits.max_conversation_turns", 10)
    if max_turns <= 0:
        return True, ""

    current_turns = count_user_turns(messages)
    if current_turns >= max_turns:
        return False, f"This conversation has reached the limit of {max_turns} turns."

    return True, ""


def check_prompt_length(prompt):
    max_length = get_int_setting("limits.max_prompt_length", 2000)
    if max_length <= 0:
        return True, ""

    if len(prompt) > max_length:
        return (
            False,
            f"Please shorten your message to {max_length} characters or fewer.",
        )

    return True, ""


def check_rate_limits(identifier):
    """Check if the user has exceeded rate limits."""
    # Simple per-session rate limit in memory
    if "request_count" not in st.session_state:
        st.session_state["request_count"] = 0
        st.session_state["last_request_time"] = datetime.now()

    if "token_count" not in st.session_state:
        st.session_state["token_count"] = 0

    # Reset if a day has passed (simplistic)
    if datetime.now() - st.session_state["last_request_time"] > timedelta(days=1):
        st.session_state["request_count"] = 0
        st.session_state["token_count"] = 0
        st.session_state["last_request_time"] = datetime.now()

    daily_limit = get_int_setting("limits.daily_request_limit", 100)
    if daily_limit > 0 and st.session_state["request_count"] >= daily_limit:
        return False, f"Daily limit of {daily_limit} requests reached."

    daily_token_limit = get_int_setting("limits.daily_token_limit", 50000)
    if daily_token_limit > 0 and st.session_state["token_count"] >= daily_token_limit:
        return False, f"Daily token budget of about {daily_token_limit} tokens reached."

    return True, ""


def log_rate_limit_event(identifier, event_type):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO rate_limit_events (identifier, event_type) VALUES (?, ?)",
        (identifier, event_type),
    )
    conn.commit()
    conn.close()


def increment_usage(identifier, tokens=0):
    st.session_state["request_count"] += 1
    st.session_state["token_count"] += max(tokens, 0)
