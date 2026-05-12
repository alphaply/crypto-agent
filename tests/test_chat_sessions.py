import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.database as database
from backend.database_chat import ChatSessionStore


def create_chat_sessions_table(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        '''
        CREATE TABLE chat_sessions (
            session_id TEXT PRIMARY KEY,
            title TEXT,
            config_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        '''
    )
    conn.commit()
    conn.close()


class ChatSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "chat.db"
        create_chat_sessions_table(self.db_path)
        self.db_patch = patch.object(database, "DB_NAME", str(self.db_path))
        self.db_patch.start()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_chat_session_crud_round_trip(self):
        store = ChatSessionStore(database.get_db_conn, lambda: "2026-05-12 10:00:00")
        with patch.object(database, "_chat_session_store", store):
            database.create_chat_session("s1", "cfg-a", "BTC/USDT", "first title")
            database.update_chat_session_title("s1", "renamed")
            database.touch_chat_session("s1")

            row = database.get_chat_session("s1")
            rows = database.get_chat_sessions(limit=10)

        self.assertEqual(row["session_id"], "s1")
        self.assertEqual(row["config_id"], "cfg-a")
        self.assertEqual(row["symbol"], "BTC/USDT")
        self.assertEqual(row["title"], "renamed")
        self.assertEqual(row["updated_at"], "2026-05-12 10:00:00")
        self.assertEqual(len(rows), 1)

    def test_delete_chat_sessions_filters_empty_ids(self):
        store = ChatSessionStore(database.get_db_conn, lambda: "2026-05-12 11:00:00")
        with patch.object(database, "_chat_session_store", store):
            database.create_chat_session("s1", "cfg-a", "BTC/USDT", "first")
            database.create_chat_session("s2", "cfg-b", "ETH/USDT", "second")

            deleted = database.delete_chat_sessions(["s1", "", None, "s2"])
            rows = database.get_chat_sessions(limit=10)

        self.assertEqual(deleted, 2)
        self.assertEqual(rows, [])

    def test_delete_single_chat_session_returns_deleted_count(self):
        store = ChatSessionStore(database.get_db_conn, lambda: "2026-05-12 12:00:00")
        with patch.object(database, "_chat_session_store", store):
            database.create_chat_session("s1", "cfg-a", "BTC/USDT", "first")

            deleted = database.delete_chat_session("s1")
            missing = database.get_chat_session("s1")

        self.assertEqual(deleted, 1)
        self.assertIsNone(missing)


if __name__ == "__main__":
    unittest.main()