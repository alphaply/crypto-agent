import json
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime


class PositionHistoryStore:
    def __init__(self, conn_factory: Callable[[], AbstractContextManager], now_factory: Callable[[], datetime], logger):
        self._conn_factory = conn_factory
        self._now_factory = now_factory
        self._logger = logger

    def _timestamp(self) -> str:
        return self._now_factory().strftime("%Y-%m-%d %H:%M:%S")

    def upsert(
        self,
        config_id,
        symbol,
        position_key,
        side=None,
        status=None,
        source=None,
        opened_at=None,
        closed_at=None,
        entry_price=None,
        close_price=None,
        amount=None,
        realized_pnl=None,
        raw=None,
    ):
        if not config_id or not position_key:
            return

        updated_at = self._timestamp()
        raw_json = json.dumps(raw or {}, ensure_ascii=False, default=str)
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO position_history (
                    config_id, symbol, position_key, side, status, source, opened_at,
                    closed_at, entry_price, close_price, amount, realized_pnl, raw_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_id, position_key) DO UPDATE SET
                    symbol = excluded.symbol,
                    side = COALESCE(excluded.side, position_history.side),
                    status = CASE
                        WHEN position_history.status = 'CLOSED' AND excluded.status = 'OPEN' THEN position_history.status
                        ELSE COALESCE(excluded.status, position_history.status)
                    END,
                    source = COALESCE(excluded.source, position_history.source),
                    opened_at = COALESCE(position_history.opened_at, excluded.opened_at),
                    closed_at = COALESCE(excluded.closed_at, position_history.closed_at),
                    entry_price = COALESCE(excluded.entry_price, position_history.entry_price),
                    close_price = COALESCE(excluded.close_price, position_history.close_price),
                    amount = COALESCE(excluded.amount, position_history.amount),
                    realized_pnl = COALESCE(excluded.realized_pnl, position_history.realized_pnl),
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                ''',
                (
                    str(config_id),
                    symbol,
                    str(position_key),
                    side,
                    status,
                    source,
                    opened_at,
                    closed_at,
                    entry_price,
                    close_price,
                    amount,
                    realized_pnl,
                    raw_json,
                    updated_at,
                ),
            )
            conn.commit()

    def list(self, config_id, since_time=None, limit=50):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            params = [config_id]
            where = "WHERE config_id = ?"
            if since_time:
                where += " AND (updated_at >= ? OR closed_at >= ? OR opened_at >= ?)"
                params.extend([since_time, since_time, since_time])
            params.append(int(limit or 50))
            rows = cursor.execute(
                f'''
                SELECT config_id, symbol, position_key, side, status, source, opened_at, closed_at,
                       entry_price, close_price, amount, realized_pnl, updated_at
                FROM position_history
                {where}
                ORDER BY COALESCE(closed_at, updated_at, opened_at) DESC
                LIMIT ?
                ''',
                tuple(params),
            ).fetchall()
            return [dict(row) for row in rows]

    def sync_open_positions(self, config_id, symbol, positions, source="exchange_position"):
        for position in positions or []:
            try:
                side = str(position.get("side") or "").upper() or "UNKNOWN"
                amount = float(position.get("amount", position.get("contracts", 0)) or 0)
                entry_price = float(position.get("entry_price", position.get("entryPrice", 0)) or 0)
                if amount <= 0:
                    continue
                position_key = f"{source}:{symbol}:{side}:{entry_price}"
                self.upsert(
                    config_id=config_id,
                    symbol=symbol,
                    position_key=position_key,
                    side=side,
                    status="OPEN",
                    source=source,
                    entry_price=entry_price,
                    amount=amount,
                    raw=position,
                )
            except Exception as exc:
                self._logger.warning(f"Failed to sync open position history: {exc}")

    def sync_trade_positions(self, config_id, symbol, trades, source="exchange_trade"):
        for trade in trades or []:
            try:
                info = trade.get("info") or {}
                raw_pnl = trade.get("realizedPnl")
                if raw_pnl is None:
                    raw_pnl = info.get("realizedPnl")
                realized_pnl = float(raw_pnl or 0)
                if realized_pnl == 0:
                    continue

                timestamp = trade.get("timestamp")
                closed_at = None
                if timestamp:
                    closed_at = self._now_factory().fromtimestamp(float(timestamp) / 1000, self._now_factory().tzinfo).strftime("%Y-%m-%d %H:%M:%S")
                side = str(trade.get("side") or info.get("side") or "").upper()
                position_side = str(info.get("positionSide") or "").upper()
                if position_side in {"LONG", "SHORT"}:
                    side = position_side
                position_key = f"{source}:{trade.get('id') or trade.get('order') or uuid.uuid4().hex}"
                self.upsert(
                    config_id=config_id,
                    symbol=trade.get("symbol") or symbol,
                    position_key=position_key,
                    side=side,
                    status="CLOSED",
                    source=source,
                    closed_at=closed_at,
                    close_price=float(trade.get("price", 0) or 0),
                    amount=float(trade.get("amount", 0) or 0),
                    realized_pnl=realized_pnl,
                    raw=trade,
                )
            except Exception as exc:
                self._logger.warning(f"Failed to sync trade position history: {exc}")