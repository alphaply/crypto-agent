"""Set a spot valuation starting point without changing trades or budgets."""
from datetime import datetime, timezone
import json
import math

from backend import database, storage
from backend.config import config as global_config
from backend.database.spot_cost_basis import read_spot_buy_totals, read_spot_unsettled_buy_ids
from backend.utils.market_data import MarketTool
from backend.utils.spot_config_guard import (
    SpotConfigConflict, serialized_spot_execution, spot_cost_basis_account_identity,
)
from backend.utils.spot_portfolio import get_config_symbols


class SpotCostBasisUnavailable(ValueError):
    pass


def _assert_reconciled_buy_evidence(conn, config_id: str, symbol: str) -> None:
    missing = conn.execute('''SELECT 1 FROM spot_budget_reservations r
        WHERE r.config_id=%s AND r.symbol=%s
          AND r.status IN ('reserved','unknown','submitted')
          AND COALESCE(r.reservation_kind,'buy')='buy'
          AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.config_id=r.config_id
            AND o.symbol=r.symbol AND o.order_id=r.order_id AND o.trade_mode='SPOT_DCA'
            AND COALESCE(o.event_type,'ORDER_CREATED')='ORDER_CREATED'
            AND UPPER(o.side) IN ('BUY','BUY_LIMIT')) LIMIT 1''', (config_id, symbol)).fetchone()
    if missing:
        raise SpotCostBasisUnavailable('该币种还有买入结果待核验，请稍后重试；持仓成本未修改。')


def _sync_pending_buys(config_id: str, symbol: str) -> None:
    from backend.app.services.spot_dashboard_service import (
        SpotStatisticsDependencies, _synchronize_orders,
    )

    with database.get_db_conn() as conn:
        _assert_reconciled_buy_evidence(conn, config_id, symbol)
        pending = read_spot_unsettled_buy_ids(conn, config_id, symbol, limit=1)
    if not pending:
        return
    dependencies = SpotStatisticsDependencies(
        connection_factory=database.get_db_conn,
        market_factory=MarketTool,
        fill_writer=database.upsert_spot_order_fill,
        trade_writer=database.save_trade_history,
        snapshot_writer=database.save_dca_daily_snapshot,
        cache={}, cache_ttl=0,
    )
    errors = []
    try:
        _synchronize_orders(
            config_id, symbol, MarketTool(config_id=config_id), errors,
            dependencies, for_cost_basis=True,
        )
    except storage.Error:
        raise
    except Exception as exc:
        raise SpotCostBasisUnavailable('暂时无法核对已有买入成交，请稍后重试；持仓成本未修改。') from exc
    if errors:
        raise SpotCostBasisUnavailable('部分买入委托尚未核验，请稍后重试；持仓成本未修改。')


@serialized_spot_execution
def update_spot_cost_basis(config_id: str, symbol: str, quantity: float, avg_cost: float) -> dict:
    if any(not math.isfinite(value) or value < 0 for value in (quantity, avg_cost)):
        raise ValueError('持仓数量和平均成本价必须是有限的非负数。')
    if quantity > 0 and avg_cost <= 0:
        raise ValueError('持仓数量大于零时，请填写大于零的平均成本价。')
    if not math.isfinite(quantity * avg_cost):
        raise ValueError('持仓总成本超出可计算范围。')
    cfg = global_config.get_config_by_id(config_id)
    if not cfg:
        raise FileNotFoundError('找不到该任务。')
    if cfg.get('mode') != 'SPOT_DCA':
        raise ValueError('仅现货定投任务支持修改持仓成本。')
    if symbol not in get_config_symbols(cfg):
        raise ValueError('该币种不在当前任务中。')
    runtime_credentials = {field: getattr(global_config, field, None) for field in (
        'global_binance_api_key', 'global_binance_secret', 'global_okx_api_key',
        'global_okx_secret', 'global_okx_passphrase',
    )}
    expected_account = spot_cost_basis_account_identity(runtime_credentials, cfg)

    _sync_pending_buys(config_id, symbol)
    with database.get_db_conn() as conn:
        # Config saves and deletion use the same database scope. Check again
        # after exchange reads so an in-flight edit cannot detach this basis.
        storage.lock_scope(conn, 'runtime-config')
        storage.lock_scope(conn, 'spot-budget', config_id)
        _assert_reconciled_buy_evidence(conn, config_id, symbol)
        from backend.config_store import load_runtime_snapshot
        snapshot = load_runtime_snapshot(connection=conn, raise_on_error=True) or {}
        current = next((item for item in snapshot.get('agents', [])
                        if item.get('config_id') == config_id), None)
        if not current:
            raise FileNotFoundError('该任务已被删除，请刷新页面。')
        if (current.get('mode') != 'SPOT_DCA' or symbol not in get_config_symbols(current)
                or spot_cost_basis_account_identity(snapshot, current) != expected_account):
            raise SpotConfigConflict('任务的交易账户或币种已变更，请刷新后重试。')
        orders = read_spot_buy_totals(conn, config_id, symbol)
        baselines = {
            order_id: {key: order[key] for key in ('qty', 'cost', 'status')}
            for order_id, order in orders.items()
        }
        updated_at = datetime.now(timezone.utc).isoformat(timespec='microseconds')
        conn.execute('''INSERT INTO spot_cost_basis
            (config_id,symbol,quantity,avg_cost,order_baselines_json,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (config_id,symbol) DO UPDATE SET quantity=excluded.quantity,
                avg_cost=excluded.avg_cost,order_baselines_json=excluded.order_baselines_json,
                updated_at=excluded.updated_at''',
            (config_id, symbol, quantity, avg_cost, json.dumps(baselines, allow_nan=False), updated_at))
        conn.commit()
    from backend.app.services.dashboard_service import DCA_STATS_CACHE
    DCA_STATS_CACHE.pop(f'{config_id}:{symbol}', None)
    return {'symbol': symbol, 'quantity': quantity, 'avg_cost': avg_cost, 'updated_at': updated_at}
