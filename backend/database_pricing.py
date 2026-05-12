from collections.abc import Callable
from contextlib import AbstractContextManager


class PricingStore:
    def __init__(self, conn_factory: Callable[[], AbstractContextManager]):
        self._conn_factory = conn_factory

    def get_all(self):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            rows = cursor.execute("SELECT * FROM model_pricing").fetchall()
            return {row["model"]: dict(row) for row in rows}

    def upsert(self, model, input_price, output_price, currency="USD") -> None:
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO model_pricing (model, input_price_per_m, output_price_per_m, currency)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(model) DO UPDATE SET
                    input_price_per_m = excluded.input_price_per_m,
                    output_price_per_m = excluded.output_price_per_m,
                    currency = excluded.currency
                ''',
                (model, input_price, output_price, currency),
            )
            conn.commit()

    def delete(self, model) -> int:
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM model_pricing WHERE model = ?", (model,))
            deleted = cursor.rowcount
            conn.commit()
        return deleted