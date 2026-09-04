from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager


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
            columns = {row[1] for row in cursor.execute("PRAGMA table_info(chat_sessions)").fetchall()}
            if "session_type" not in columns:
                cursor.execute("ALTER TABLE chat_sessions ADD COLUMN session_type TEXT NOT NULL DEFAULT 'task'")
            if "runtime_json" not in columns:
                cursor.execute("ALTER TABLE chat_sessions ADD COLUMN runtime_json TEXT NOT NULL DEFAULT '{}'")
            if "parent_session_id" not in columns:
                cursor.execute("ALTER TABLE chat_sessions ADD COLUMN parent_session_id TEXT")
            if "root_session_id" not in columns:
                cursor.execute("ALTER TABLE chat_sessions ADD COLUMN root_session_id TEXT")
            if "fork_message_index" not in columns:
                cursor.execute("ALTER TABLE chat_sessions ADD COLUMN fork_message_index INTEGER")
            cursor.execute(
                '''
                INSERT INTO chat_sessions (
                    session_id, title, config_id, symbol, session_type, runtime_json,
                    parent_session_id, root_session_id, fork_message_index, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                "UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            conn.commit()

    def get_session(self, session_id: str):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            row = cursor.execute(
                "SELECT * FROM chat_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_sessions(self, limit: int = 100):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            rows = cursor.execute(
                "SELECT * FROM chat_sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def update_title(self, session_id: str, title: str) -> None:
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE chat_sessions SET title = ? WHERE session_id = ?",
                (title, session_id),
            )
            conn.commit()

    def delete_session(self, session_id: str) -> int:
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,))
            deleted = cursor.rowcount
            conn.commit()
            return deleted

    def delete_sessions(self, session_ids: Iterable[str]) -> int:
        ids = [session_id for session_id in session_ids if session_id]
        if not ids:
            return 0
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            placeholders = ",".join(["?"] * len(ids))
            cursor.execute(f"DELETE FROM chat_sessions WHERE session_id IN ({placeholders})", tuple(ids))
            deleted = cursor.rowcount
            conn.commit()
            return deleted
