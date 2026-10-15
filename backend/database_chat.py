from backend import storage
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from uuid import uuid4


class ChatSessionStore:
    def __init__(self, conn_factory: Callable[[], AbstractContextManager], now_factory: Callable[[], str]):
        self._conn_factory = conn_factory
        self._now_factory = now_factory

    def create_session(
        self,
        session_id: str,
        config_id: str,
        symbol: str,
        title: str,
        session_type: str = "task",
        runtime_json: str = "{}",
        parent_session_id: str | None = None,
        root_session_id: str | None = None,
        fork_message_index: int | None = None,
    ) -> None:
        now = self._now_factory()
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO chat_sessions (
                    session_id, title, config_id, symbol, session_type, runtime_json,
                    parent_session_id, root_session_id, fork_message_index, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''',
                (
                    session_id,
                    title,
                    config_id,
                    symbol,
                    session_type,
                    runtime_json,
                    parent_session_id,
                    root_session_id or session_id,
                    fork_message_index,
                    now,
                    now,
                ),
            )
            conn.commit()

    def touch_session(self, session_id: str) -> None:
        now = self._now_factory()
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE chat_sessions SET updated_at = %s WHERE session_id = %s",
                (now, session_id),
            )
            conn.commit()

    def get_session(self, session_id: str):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            row = cursor.execute(
                "SELECT * FROM chat_sessions WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            return storage.record(row) if row else None

    def list_sessions(self, limit: int = 100):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            rows = cursor.execute(
                "SELECT * FROM chat_sessions ORDER BY updated_at DESC NULLS LAST LIMIT %s",
                (limit,),
            ).fetchall()
            return [storage.record(row) for row in rows]

    def update_title(self, session_id: str, title: str, *, summary_token: str | None = None) -> bool:
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            condition = " AND title_summary_token = %s" if summary_token is not None else ""
            params = (title, session_id, summary_token) if summary_token is not None else (title, session_id)
            cursor.execute(
                "UPDATE chat_sessions SET title = %s WHERE session_id = %s" + condition,
                params,
            )
            conn.commit()
            return bool(cursor.rowcount)

    def claim_title_summary(self, session_id: str, summary_cursor: int) -> str | None:
        """Claim one title attempt per persisted compaction, including failed attempts."""
        if summary_cursor <= 0:
            return None
        token = uuid4().hex
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE chat_sessions SET title_summary_cursor = %s, title_summary_token = %s "
                "WHERE session_id = %s AND title_summary_cursor < %s",
                (summary_cursor, token, session_id, summary_cursor),
            )
            conn.commit()
            return token if cursor.rowcount else None

    def reset_title_summary(self, session_id: str) -> None:
        with self._conn_factory() as conn:
            conn.execute(
                "UPDATE chat_sessions SET title_summary_cursor = 0, title_summary_token = '' WHERE session_id = %s",
                (session_id,),
            )
            conn.commit()

    def delete_session(self, session_id: str) -> int:
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM chat_sessions WHERE session_id = %s", (session_id,))
            deleted = cursor.rowcount
            conn.commit()
            return deleted

    def delete_sessions(self, session_ids: Iterable[str]) -> int:
        ids = [session_id for session_id in session_ids if session_id]
        if not ids:
            return 0
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            placeholders = ",".join(["%s"] * len(ids))
            cursor.execute(f"DELETE FROM chat_sessions WHERE session_id IN ({placeholders})", tuple(ids))
            deleted = cursor.rowcount
            conn.commit()
            return deleted
