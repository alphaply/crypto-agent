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


def spot_cost_basis_account_identity(snapshot: dict, agent: dict) -> str:
    """Fingerprint the exchange account in a resolved runtime snapshot.

    Provider-resolved ``api_key`` can belong to the LLM. Prefer the bound
    exchange profile and exchange-specific fields, and only interpret the
    generic legacy key as an exchange key when no LLM provider is bound.
    """
    profile_id = str(agent.get('exchange_profile_id') or '')
    profile = next((item for item in snapshot.get('exchange_profiles') or []
                    if str(item.get('profile_id') or '') == profile_id), {}) if profile_id else {}
    exchange = str(profile.get('exchange') or agent.get('exchange') or 'binance').lower()
    prefix = 'okx' if exchange == 'okx' else 'binance'
    key = profile.get('api_key') or agent.get(f'{prefix}_api_key')
    secret = profile.get('secret') or agent.get(f'{prefix}_secret')
    if not profile_id:
        if not agent.get('llm_provider_id'):
            key = key or agent.get('api_key')
        secret = secret or agent.get('secret')
    passphrase = profile.get('passphrase') or agent.get('passphrase')
    if not key or not secret:
        key = snapshot.get(f'global_{prefix}_api_key')
        secret = snapshot.get(f'global_{prefix}_secret')
        passphrase = snapshot.get('global_okx_passphrase') if exchange == 'okx' else None
    elif exchange == 'okx' and not passphrase:
        passphrase = snapshot.get('global_okx_passphrase')
    if exchange != 'okx':
        passphrase = None
    identity = [
        profile_id, exchange, profile.get('market_type') or agent.get('market_type') or 'spot',
        key or None, secret or None, passphrase or None,
    ]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()


def assert_spot_cost_basis_snapshot_change_allowed(conn, previous: dict | None, updated: dict) -> None:
    """Recheck persisted bases while the caller holds the runtime-config lock.

    An earlier API check can race another process saving a new cost basis.
    Explicit task deletion removes its basis in the same transaction before
    this check; ordinary saves and imports must keep its task and account.
    """
    rows = conn.execute('SELECT config_id,symbol FROM spot_cost_basis').fetchall()
    if not rows:
        return
    previous = previous or {}
    before = {item.get('config_id'): item for item in previous.get('agents') or []}
    after = {item.get('config_id'): item for item in updated.get('agents') or []}
    for row in rows:
        old_agent = before.get(row['config_id'])
        new_agent = after.get(row['config_id'])
        if (not old_agent or not new_agent
                or str(new_agent.get('mode') or '').upper() != 'SPOT_DCA'
                or row['symbol'] not in get_config_symbols(new_agent)):
            raise SpotConfigConflict('现货任务有已保存的持仓成本，不能移除币种或切换模式；删除任务请使用删除按钮。')
        if spot_cost_basis_account_identity(previous, old_agent) != spot_cost_basis_account_identity(updated, new_agent):
            raise SpotConfigConflict('现货任务有已保存的持仓成本，不能切换交易账户或凭据；请新建任务。')


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
    from backend.utils.visible_management import retired_management_resources
    retired = retired_management_resources(config_id)
    with database.get_db_conn() as conn:
        tables = {row[0] for row in conn.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname=current_schema()")}
        affected = set()
        if 'spot_cost_basis' in tables:
            affected.update(row[0] for row in conn.execute(
                'SELECT symbol FROM spot_cost_basis WHERE config_id=%s', (config_id,)))
        if 'orders' in tables:
            fill_join = ('LEFT JOIN spot_order_fills f ON f.config_id=o.config_id AND f.symbol=o.symbol '
                         'AND f.order_id=o.order_id') if 'spot_order_fills' in tables else ''
            fill_columns = 'f.status AS verified_status,f.filled_qty AS verified_qty,f.filled_cost AS verified_cost' if fill_join else 'NULL AS verified_status,NULL AS verified_qty,NULL AS verified_cost'
            rows = conn.execute(f'''SELECT o.symbol,o.status,o.filled_amount,o.filled_cost,{fill_columns}
                FROM orders o {fill_join} WHERE o.config_id=%s AND o.trade_mode='SPOT_DCA'
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
                WHERE config_id=%s AND status<>'released' ''', (config_id,)).fetchall()
            for row in rows:
                empty = conn.execute('''SELECT 1 FROM spot_order_fills
                    WHERE config_id=%s AND symbol=%s AND order_id=%s
                      AND UPPER(status) IN ('CANCELLED','CANCELED','EXPIRED','REJECTED')
                      AND COALESCE(filled_qty,0)=0 AND COALESCE(filled_cost,0)=0''',
                    (config_id, row['symbol'], row['order_id'])).fetchone() if 'spot_order_fills' in tables else None
                if not empty:
                    affected.add(row['symbol'])
        if 'trade_action_runs' in tables:
            affected.update(row[0] for row in conn.execute('''SELECT symbol FROM trade_action_runs
                WHERE config_id=%s AND status IN ('running','unknown','pending')''', (config_id,)))
    if any(float(previous.get(key) or 0) != 0 for key in ('initial_qty', 'initial_cost', 'manual_avg_cost')):
        affected.add(previous.get('symbol'))
    # Confirmed release / accepted transfer already reconciled the physical
    # stock. Historical buy attribution stays auditable after task retirement.
    affected = {symbol for symbol in affected if str(symbol).split('/')[0] not in retired}
    if affected and (account_changed or mode_changed):
        raise SpotConfigConflict('现货任务仍有持仓、委托或待核验记录，不能切换交易账户或模式；请新建任务')
    blocked = affected & removed
    if blocked:
        raise SpotConfigConflict('以下现货标的仍有持仓、委托或待核验记录，不能移出任务：' + ', '.join(sorted(blocked)))
