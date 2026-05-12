from collections.abc import Callable
from contextlib import AbstractContextManager


class ConfigCleanupStore:
    def __init__(self, conn_factory: Callable[[], AbstractContextManager], now_factory: Callable[[], str]):
        self._conn_factory = conn_factory
        self._now_factory = now_factory

    def get_dependency_counts(self, config_id: str):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            tables = [
                "chat_sessions",
                "mock_accounts",
                "mock_balance_history",
                "mock_orders",
                "orders",
                "summaries",
                "token_usage",
                "daily_summaries",
                "short_memories",
                "position_history",
            ]
            counts = {}
            for table in tables:
                counts[table] = cursor.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE config_id = ?",
                    (config_id,),
                ).fetchone()[0]

            counts["open_mock_orders"] = cursor.execute(
                "SELECT COUNT(*) FROM mock_orders WHERE config_id = ? AND status = 'OPEN'",
                (config_id,),
            ).fetchone()[0]
            counts["open_orders"] = cursor.execute(
                "SELECT COUNT(*) FROM orders WHERE config_id = ? AND status = 'OPEN'",
                (config_id,),
            ).fetchone()[0]
            return counts

    def soft_delete_runtime_data(self, config_id: str):
        with self._conn_factory() as conn:
            cursor = conn.cursor()

            deleted_chat_sessions = cursor.execute(
                "DELETE FROM chat_sessions WHERE config_id = ?",
                (config_id,),
            ).rowcount
            deleted_mock_accounts = cursor.execute(
                "DELETE FROM mock_accounts WHERE config_id = ?",
                (config_id,),
            ).rowcount
            deleted_mock_balance_history = cursor.execute(
                "DELETE FROM mock_balance_history WHERE config_id = ?",
                (config_id,),
            ).rowcount

            closed_open_mock_orders = cursor.execute(
                "UPDATE mock_orders SET status = 'CLOSED', close_time = ? WHERE config_id = ? AND status = 'OPEN'",
                (self._now_factory(), config_id),
            ).rowcount
            cancelled_open_orders = cursor.execute(
                "UPDATE orders SET status = 'CANCELLED' WHERE config_id = ? AND status = 'OPEN'",
                (config_id,),
            ).rowcount

            conn.commit()

            return {
                "chat_sessions_deleted": deleted_chat_sessions,
                "mock_accounts_deleted": deleted_mock_accounts,
                "mock_balance_history_deleted": deleted_mock_balance_history,
                "open_mock_orders_closed": closed_open_mock_orders,
                "open_orders_cancelled": cancelled_open_orders,
            }

    def purge_all_data(self, config_id: str):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cleanup = {
                "chat_sessions_deleted": cursor.execute(
                    "DELETE FROM chat_sessions WHERE config_id = ?",
                    (config_id,),
                ).rowcount,
                "mock_accounts_deleted": cursor.execute(
                    "DELETE FROM mock_accounts WHERE config_id = ?",
                    (config_id,),
                ).rowcount,
                "mock_balance_history_deleted": cursor.execute(
                    "DELETE FROM mock_balance_history WHERE config_id = ?",
                    (config_id,),
                ).rowcount,
                "mock_orders_deleted": cursor.execute(
                    "DELETE FROM mock_orders WHERE config_id = ?",
                    (config_id,),
                ).rowcount,
                "orders_deleted": cursor.execute(
                    "DELETE FROM orders WHERE config_id = ?",
                    (config_id,),
                ).rowcount,
                "summaries_deleted": cursor.execute(
                    "DELETE FROM summaries WHERE config_id = ?",
                    (config_id,),
                ).rowcount,
                "token_usage_deleted": cursor.execute(
                    "DELETE FROM token_usage WHERE config_id = ?",
                    (config_id,),
                ).rowcount,
                "daily_summaries_deleted": cursor.execute(
                    "DELETE FROM daily_summaries WHERE config_id = ?",
                    (config_id,),
                ).rowcount,
                "short_memories_deleted": cursor.execute(
                    "DELETE FROM short_memories WHERE config_id = ?",
                    (config_id,),
                ).rowcount,
                "position_history_deleted": cursor.execute(
                    "DELETE FROM position_history WHERE config_id = ?",
                    (config_id,),
                ).rowcount,
            }

            conn.commit()
            return cleanup