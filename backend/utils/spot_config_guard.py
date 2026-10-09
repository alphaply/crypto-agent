"""Prevent configuration edits from detaching live spot funds or order evidence."""
from functools import wraps
import hashlib
import json
import threading

from backend import database
from backend.utils.spot_portfolio import get_config_symbols


class SpotConfigConflict(ValueError):
    pass


spot_config_lock = threading.RLock()


def spot_execution_fingerprint(config: dict) -> str:
    from backend.config import config as runtime_config

    keys = ('mode', 'enabled', 'symbol', 'exchange', 'exchange_profile_id', 'dca_amount',
            'dca_budget', 'dca_freq', 'api_key', 'secret', 'passphrase', 'password',
            'binance_api_key', 'binance_secret', 'okx_api_key', 'okx_secret', 'okx_passphrase')
    payload = {key: config.get(key) for key in keys}
    if config.get('mcp_symbol_scope') == 'all':
        # Request-local symbols and accumulated ownership do not alter policy.
        payload['symbol'] = None
        payload['symbols'] = []
        payload['mcp_symbol_scope'] = 'all'
    else:
        payload['symbols'] = get_config_symbols(config)
    # Mirror Config.get_exchange_credentials using the captured task plus live
    # fallback values. The caller stores only this hash before model inference.
    exchange = str(config.get('exchange') or 'binance').lower()
    prefix = 'okx' if exchange == 'okx' else 'binance'
    key = config.get(f'{prefix}_api_key') or config.get('api_key')
    secret = config.get(f'{prefix}_secret') or config.get('secret')
    passphrase = config.get('passphrase') if exchange == 'okx' else None
    if not key or not secret:
        key = getattr(runtime_config, f'global_{prefix}_api_key', None)
        secret = getattr(runtime_config, f'global_{prefix}_secret', None)
        passphrase = getattr(runtime_config, 'global_okx_passphrase', None) if exchange == 'okx' else None
    elif exchange == 'okx' and not passphrase:
        passphrase = getattr(runtime_config, 'global_okx_passphrase', None)
    payload['effective_credentials'] = [exchange, key, secret, passphrase]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str).encode()).hexdigest()


def serialized_spot_execution(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with spot_config_lock:
            return function(*args, **kwargs)
    return wrapped


def assert_spot_config_change_allowed(previous: dict | None, updated: dict | None, *, account_changed=False) -> None:
    if not previous or str(previous.get('mode') or '').upper() != 'SPOT_DCA':
        return
    updated = updated or {}
    config_id = str(previous.get('config_id') or '')
    old_symbols = set(get_config_symbols(previous))
    new_symbols = set(get_config_symbols(updated)) if str(updated.get('mode') or '').upper() == 'SPOT_DCA' else set()
    if len(old_symbols) == 1 and len(new_symbols) > 1 and any(
        float(previous.get(key) or 0) != 0 for key in ('initial_qty', 'initial_cost', 'manual_avg_cost')
    ):
        raise SpotConfigConflict('现货任务有手工初始持仓成本，请先核对并单独保存清理后的初始持仓设置，再增加标的，避免丢失成本记录')
    removed = old_symbols - new_symbols
    account_changed = account_changed or any(previous.get(key) != updated.get(key)
        for key in ('exchange_profile_id', 'exchange'))
    mode_changed = str(updated.get('mode') or '').upper() != 'SPOT_DCA'
    manual_changed = previous.get('symbol') != updated.get('symbol') and any(
        float(previous.get(key) or 0) != 0 for key in ('initial_qty', 'initial_cost', 'manual_avg_cost'))
    if manual_changed:
        raise SpotConfigConflict('现货主标的有手工持仓成本，不能通过排序更换主标的；请先核对并清理手工成本')
    if not (removed or account_changed or mode_changed):
        return
    with database.get_db_conn() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        affected = set()
        if 'orders' in tables:
            fill_join = ('LEFT JOIN spot_order_fills f ON f.config_id=o.config_id AND f.symbol=o.symbol '
                         'AND f.order_id=o.order_id') if 'spot_order_fills' in tables else ''
            fill_columns = 'f.status AS verified_status,f.filled_qty AS verified_qty,f.filled_cost AS verified_cost' if fill_join else 'NULL AS verified_status,NULL AS verified_qty,NULL AS verified_cost'
            rows = conn.execute(f'''SELECT o.symbol,o.status,o.filled_amount,o.filled_cost,{fill_columns}
                FROM orders o {fill_join} WHERE o.config_id=? AND o.trade_mode='SPOT_DCA'
                AND COALESCE(o.event_type,'ORDER_CREATED')='ORDER_CREATED'
                AND UPPER(o.side) IN ('BUY','BUY_LIMIT')''', (config_id,)).fetchall()
            for row in rows:
                status = str(row['verified_status'] or row['status'] or '').upper()
                verified_empty = (row['verified_status'] is not None
                    and status in {'CANCELLED', 'CANCELED', 'EXPIRED', 'REJECTED'}
                    and float(row['verified_qty'] or 0) == 0 and float(row['verified_cost'] or 0) == 0)
                if not verified_empty:
                    affected.add(row['symbol'])
        if 'spot_budget_reservations' in tables:
            rows = conn.execute('''SELECT symbol,order_id,status FROM spot_budget_reservations
                WHERE config_id=? AND status<>'released' ''', (config_id,)).fetchall()
            for row in rows:
                empty = conn.execute('''SELECT 1 FROM spot_order_fills
                    WHERE config_id=? AND symbol=? AND order_id=?
                      AND UPPER(status) IN ('CANCELLED','CANCELED','EXPIRED','REJECTED')
                      AND COALESCE(filled_qty,0)=0 AND COALESCE(filled_cost,0)=0''',
                    (config_id, row['symbol'], row['order_id'])).fetchone() if 'spot_order_fills' in tables else None
                if not empty:
                    affected.add(row['symbol'])
        if 'trade_action_runs' in tables:
            affected.update(row[0] for row in conn.execute('''SELECT symbol FROM trade_action_runs
                WHERE config_id=? AND status IN ('running','unknown','pending')''', (config_id,)))
    if any(float(previous.get(key) or 0) != 0 for key in ('initial_qty', 'initial_cost', 'manual_avg_cost')):
        affected.add(previous.get('symbol'))
    if affected and (account_changed or mode_changed):
        raise SpotConfigConflict('现货任务仍有持仓、委托或待核验记录，不能切换交易账户或模式；请新建任务')
    blocked = affected & removed
    if blocked:
        raise SpotConfigConflict('以下现货标的仍有持仓、委托或待核验记录，不能移出任务：' + ', '.join(sorted(blocked)))
