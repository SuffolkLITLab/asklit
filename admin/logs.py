import streamlit as st
import pandas as pd
from asklit.db import get_connection

# st.set_page_config(page_title="Usage Logs", icon="📈")


def main():
    st.title("📈 Usage Logs")

    if not st.session_state.get("is_admin_authenticated"):
        st.error("Access denied. Please login.")
        st.stop()

    tab1, tab2 = st.tabs(["Conversations", "Rate Limit Events"])

    with tab1:
        st.header("Recent Conversations")
        conn = get_connection()
        query = """
        SELECT c.id, c.title, c.created_at, COUNT(m.id) as message_count
        FROM conversations c
        LEFT JOIN messages m ON c.id = m.conversation_id
        GROUP BY c.id
        ORDER BY c.created_at DESC
        LIMIT 100
        """
        df = pd.read_sql_query(query, conn)

        if not df.empty:
            for i, row in df.iterrows():
                with st.expander(
                    f"{row['title']} ({row['created_at']}) - {row['message_count']} msgs"
                ):
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
                        (row["id"],),
                    )
                    msgs = cursor.fetchall()
                    for m in msgs:
                        st.write(f"**{m['role'].upper()}** ({m['created_at']}):")
                        st.write(m["content"])
                        st.divider()
        else:
            st.info("No conversations logged yet.")
        conn.close()

    with tab2:
        st.header("Rate Limit Events")
        conn = get_connection()
        df_rl = pd.read_sql_query(
            "SELECT * FROM rate_limit_events ORDER BY timestamp DESC LIMIT 100", conn
        )
        if not df_rl.empty:
            st.dataframe(df_rl)
        else:
            st.info("No rate limit events logged.")
        conn.close()


if __name__ == "__main__":
    main()
