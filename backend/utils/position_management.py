"""Durable enrollment of real exchange activity within explicit exclusive scopes.

Discovery only reads exchange facts and records local evidence. Trading remains
in the existing action layer, with its permissions, client IDs and receipts.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import math
import threading
import time

from backend.utils.management_policy import canonical_symbol, exclusive, normalize_management

_in_action = ContextVar('exclusive_management_action', default=None)
_in_discovery = ContextVar('exclusive_management_discovery', default=False)
_discovery_guard = threading.Lock()
_discovery_locks = {}
_discovery_success = {}


def initialize_schema(conn):
    from backend.utils.visible_management import initialize_schema as initialize_visible_schema
    initialize_visible_schema(conn)
    conn.execute('''CREATE TABLE IF NOT EXISTS position_management_scopes (
        account_scope TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
        owner TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(account_scope,symbol,side))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS position_management_facts (
        account_scope TEXT NOT NULL, symbol TEXT NOT NULL, identity TEXT NOT NULL,
        owner TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(account_scope,symbol,identity))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS position_management_actions (
        account_scope TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
        owner TEXT NOT NULL, operation_id TEXT NOT NULL, state TEXT NOT NULL,
        updated_at REAL NOT NULL, PRIMARY KEY(account_scope,symbol,side))''')


def _number(value):
    if isinstance(value, bool):
        raise ValueError('Exchange quantity is not a number')
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError('Exchange quantity is unavailable')
    return result


def _scope_active(conn, row):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'position_management_actions' in tables and conn.execute(
            "SELECT 1 FROM position_management_actions WHERE owner=? AND account_scope=? AND symbol=? "
            "AND side IN (?, 'BOTH') AND state IN ('running','unresolved','pending','unknown') LIMIT 1",
            (row['owner'], row['account_scope'], row['symbol'], row['side'])).fetchone():
        return True
    if 'real_protection_plans' in tables:
        for plan_row in conn.execute('SELECT payload FROM real_protection_plans WHERE config_id=?', (row['owner'],)):
            plan = json.loads(plan_row[0])
            if (plan.get('account_scope') == row['account_scope']
                    and canonical_symbol(plan['symbol']) == row['symbol']
                    and (row['side'] == 'BOTH' or plan.get('side') == row['side']) and plan.get('state') != 'DONE'):
                return True
    if 'trade_action_runs' in tables and conn.execute("SELECT 1 FROM trade_action_runs WHERE config_id=? "
            "AND status IN ('running','pending','unknown') LIMIT 1", (row['owner'],)).fetchone():
        return True
    profile_id = row['owner'][4:] if row['owner'].startswith('mcp:') else None
    return bool(profile_id and 'mcp_operations' in tables and conn.execute(
        "SELECT 1 FROM mcp_operations WHERE profile_id=? AND state IN ('started','unknown','pending') LIMIT 1",
        (profile_id,)).fetchone())


def configuration_candidates(conn, snapshot):
    candidates = list(snapshot.get('agents') or [])
    accounts = {a['profile_id']: a for a in snapshot.get('exchange_profiles') or []}
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'mcp_records' in tables:
        for row in conn.execute("SELECT payload FROM mcp_records WHERE kind='profile'"):
            profile = json.loads(row[0])
            account = accounts.get(profile['exchange_profile_id'], {})
            candidates.append({**account, **profile, 'config_id': 'mcp:' + profile['profile_id'], 'mode': 'REAL'})
    result = []
    for config in candidates:
        config = normalize_management(config)
        if config.get('position_management_policy') != 'exclusive_scope':
            continue
        account = accounts.get(config.get('exchange_profile_id'), {})
        if config.get('exchange_profile_id') and not account:
            raise ValueError('Exclusive management references a missing exchange account')
        exchange = str(account.get('exchange') or config.get('exchange') or 'binance').lower()
        if exchange not in {'binance', 'binanceusdm', 'okx'}:
            raise ValueError('Exclusive management only supports Binance and OKX swap accounts')
        if account and (account.get('market_type') or 'swap') != 'swap':
            raise ValueError('Exclusive management requires a swap exchange account')
        credential_exchange = 'binance' if exchange == 'binanceusdm' else exchange
        # Exchange-specific fields or saved account credentials are authoritative;
        # generic api_key can be a model provider key after runtime restoration.
        key = account.get('api_key') or config.get(credential_exchange + '_api_key') or config.get('api_key')
        secret = account.get('secret') or config.get(credential_exchange + '_secret') or config.get('secret')
        passphrase = account.get('passphrase') or config.get('passphrase')
        if not account and not (key and secret):
            key = snapshot.get('global_' + credential_exchange + '_api_key') or key
            secret = snapshot.get('global_' + credential_exchange + '_secret') or secret
            passphrase = snapshot.get('global_okx_passphrase') if credential_exchange == 'okx' else None
        if not key:
            raise ValueError('Exclusive management account credentials are unavailable')
        exchange_id = 'binanceusdm' if credential_exchange == 'binance' else exchange
        scope = hashlib.sha256(f'{exchange_id}:{key}'.encode()).hexdigest()
        revision = hashlib.sha256(json.dumps([exchange_id, key, secret, passphrase]).encode()).hexdigest()
        policy_revision = hashlib.sha256(json.dumps({
            'owner': config['config_id'], 'policy': 'exclusive_scope', 'account_scope': scope,
            'credential_revision': revision, 'market_type': 'swap', 'exit_mode': 'independent_exits',
            'scopes': sorted(config['management_scopes'], key=lambda item: (item['symbol'], item['side'])),
        }, sort_keys=True).encode()).hexdigest()
        for item in config['management_scopes']:
            result.append({**item, 'owner': config['config_id'], 'account_scope': scope,
                           'credential_revision': revision, 'policy_revision': policy_revision})
    return result


def synchronize_configuration(conn, snapshot):
    """Caller holds the configuration transaction; no network or separate commit."""
    desired = configuration_candidates(conn, snapshot)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'position_management_scopes' not in tables:
        if not desired:
            return
        initialize_schema(conn)
        tables.update({'position_management_scopes','position_management_facts','position_management_actions'})
    existing = [dict(row) for row in conn.execute('SELECT * FROM position_management_scopes')]
    wanted = {(r['account_scope'], r['symbol'], r['side'], r['owner']) for r in desired}
    candidates = list(snapshot.get('agents') or [])
    if 'mcp_records' in tables:
        candidates.extend({**json.loads(r[0]), 'config_id': 'mcp:' + json.loads(r[0])['profile_id']}
                          for r in conn.execute("SELECT payload FROM mcp_records WHERE kind='profile'"))
    configs = {c.get('config_id'): c for c in candidates}
    accounts = {a['profile_id']: a for a in snapshot.get('exchange_profiles') or []}
    for config in configs.values():
        account = accounts.get(config.get('exchange_profile_id'), config)
        venue = str(account.get('exchange', config.get('exchange', 'binance'))).replace('binanceusdm', 'binance')
        key = account.get('api_key') or config.get(venue + '_api_key') or snapshot.get('global_' + venue + '_api_key')
        explicit = account.get('physical_account_id') or config.get('physical_account_id')
        if explicit or key:
            config['_management_account_identity'] = (f'physical:{venue}:{explicit}' if explicit else
                                                     'credentials:' + hashlib.sha256(f'{venue}:{key}'.encode()).hexdigest())
    from backend.utils.visible_management import migrate_existing_claims, assert_configuration_claims
    migrate_existing_claims(conn, snapshot)
    for row in conn.execute("SELECT DISTINCT owner FROM visible_management_claims WHERE state='owned'"):
        owner_config = configs.get(row['owner'])
        assert_configuration_claims(conn, row['owner'], owner_config)
    for row in existing:
        if (configs.get(row['owner']) or {}).get('position_management_policy') == 'visible_first_claim':
            wanted.add((row['account_scope'], row['symbol'], row['side'], row['owner']))
    def known_hedged(row):
        observations = [json.loads(r['payload']).get('hedged') for r in existing
                        if r['account_scope'] == row['account_scope'] and r['symbol'] == row['symbol']]
        return bool(observations) and all(value is True for value in observations)
    # Validate every retirement/conflict before altering any scope, including
    # callers that catch an exception inside their surrounding transaction.
    for row in existing:
        replacement = next((r for r in desired if all(r[k] == row[k] for k in ('account_scope','symbol','side','owner'))), None)
        old_revision = json.loads(row['payload']).get('credential_revision')
        changed_credentials = replacement and old_revision and old_revision != replacement['credential_revision']
        if ((row['account_scope'], row['symbol'], row['side'], row['owner']) not in wanted or changed_credentials) and _scope_active(conn, row):
            raise ValueError('Exclusive management has active positions, orders or unresolved operations; resolve before changing scope/account credentials')
    for index, row in enumerate(desired):
        for other in desired[:index]:
            if other['account_scope'] == row['account_scope'] and other['symbol'] == row['symbol'] and other['owner'] != row['owner']:
                if other['side'] == row['side'] or not known_hedged(row):
                    raise ValueError('Another task/profile has an overlapping exclusive management scope')
        if 'real_protection_plans' in tables:
            for stored in conn.execute('SELECT config_id,payload FROM real_protection_plans WHERE symbol IN (?,?)',
                                       (row['symbol'], row['symbol'].split(':')[0])):
                plan = json.loads(stored['payload'])
                if (plan.get('account_scope') == row['account_scope'] and stored['config_id'] != row['owner']
                        and plan.get('state') != 'DONE' and (plan.get('side') == row['side'] or not known_hedged(row))):
                    raise ValueError('Another task owns an existing position cycle in this scope')
    for row in existing:
        if (row['account_scope'], row['symbol'], row['side'], row['owner']) not in wanted:
            conn.execute('DELETE FROM position_management_scopes WHERE account_scope=? AND symbol=? AND side=?',
                         (row['account_scope'], row['symbol'], row['side']))
            conn.execute("UPDATE visible_management_claims SET state='released',version=version+1 WHERE owner=? AND resource=? "
                         "AND json_extract(payload,'$.source')='migration' AND json_extract(payload,'$.legacy_policy')='exclusive_scope' "
                         "AND json_extract(payload,'$.resource_bound')=0",
                         (row['owner'], row['symbol']))
    for row in desired:
        previous = next((r for r in existing if all(r[k] == row[k] for k in ('account_scope', 'symbol', 'side', 'owner'))), None)
        payload = json.loads(previous['payload']) if previous else {'policy': 'exclusive_scope', 'sync_status': 'pending'}
        payload['credential_revision'] = row['credential_revision']
        payload['policy_revision'] = row['policy_revision']
        payload['authorization_source'] = 'configuration'
        conn.execute('INSERT INTO position_management_scopes VALUES(?,?,?,?,?) ON CONFLICT(account_scope,symbol,side) '
                     'DO UPDATE SET owner=excluded.owner,payload=excluded.payload',
                     (row['account_scope'], row['symbol'], row['side'], row['owner'], json.dumps(payload)))


def _configuration(service):
    config = getattr(service.mt, 'runtime_config', None)
    if config:
        from backend.utils.visible_management import _active_management
        active = _active_management.get()
        if active and active.get('owner') == service.config_id:
            from backend.utils.visible_management import physical_account_scope
            if active['physical_account'] != physical_account_scope(service.mt):
                raise ValueError('Runtime account changed inside the financial management context')
            return {**config, 'position_management_policy': 'visible_first_claim', 'management_scopes': []}
        return config
    from backend.config import config as runtime
    restored = runtime.get_config_by_id(service.config_id) or {}
    if not hasattr(service.mt, 'runtime_config'):
        # Old injected/compatibility clients have no trusted runtime witness for
        # automatic management. Actual MarketTool and v3 contexts bind it.
        restored = {**restored, 'position_management_policy': 'strict_owned', 'management_scopes': []}
    return restored


def enabled(service, symbol, side=None):
    return exclusive(_configuration(service), symbol, side)


def _claim(service, symbol, side, hedged):
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = list(conn.execute('SELECT * FROM position_management_scopes WHERE account_scope=? AND symbol=?',
                                 (service.account_scope, symbol)))
        for row in rows:
            if row['owner'] != service.config_id and (not hedged or row['side'] == side):
                raise ValueError('Another manager owns this exchange position scope')
        for row in conn.execute('SELECT config_id,payload FROM real_protection_plans WHERE symbol IN (?,?)',
                                 (symbol, symbol.split(':')[0])):
            plan = json.loads(row['payload'])
            if (plan.get('account_scope') == service.account_scope and row['config_id'] != service.config_id
                    and plan.get('state') != 'DONE' and (not hedged or plan.get('side') == side)):
                raise ValueError('Another task owns an active position cycle')
        own = next((r for r in rows if r['side'] == side), None)
        payload = json.loads(own['payload']) if own else {'policy': 'exclusive_scope', 'sync_status': 'pending'}
        payload['hedged'] = hedged
        conn.execute('INSERT INTO position_management_scopes VALUES(?,?,?,?,?) ON CONFLICT(account_scope,symbol,side) '
                     'DO UPDATE SET payload=excluded.payload',
                     (service.account_scope, symbol, side, service.config_id, json.dumps(payload)))
        conn.commit()


def _orders(exchange, symbol):
    from backend.utils.position_adoption import OKX_ALGO_ORDER_TYPES
    result = {}
    queries = [('regular', {})]
    queries += [('algo', {'trigger': True, 'ordType': kind}) for kind in OKX_ALGO_ORDER_TYPES] if exchange.id == 'okx' else [('algo', {'trigger': True})]
    for channel, params in queries:
        rows = exchange.fetch_open_orders(symbol, params=params)
        if not isinstance(rows, list):
            raise ValueError('Order channel is unavailable')
        for row in rows:
            if not isinstance(row, dict) or not row.get('id'):
                raise ValueError('External order identity is unavailable')
            if row.get('symbol'):
                if canonical_symbol(row['symbol']) != canonical_symbol(symbol):
                    raise ValueError('External order symbol is outside the requested management scope')
            else:
                native_symbol = (row.get('info') or {}).get('symbol') or (row.get('info') or {}).get('instId')
                if not native_symbol or native_symbol != exchange.market(symbol).get('id'):
                    raise ValueError('External order symbol scope is unavailable')
            result[f"{channel}:{row['id']}"] = {**row, '_channel': channel}
    return result


def _order_side(order, hedged, position_side=None):
    info = order.get('info') or {}
    native = str(info.get('positionSide') or info.get('posSide') or order.get('positionSide') or '').upper()
    action = str(order.get('side') or info.get('side') or '').lower()
    if action not in {'buy', 'sell'}:
        raise ValueError('External order action is unavailable')
    reduce = str(order.get('reduceOnly', info.get('reduceOnly', False))).lower() == 'true'
    all_close = str(info.get('closePosition', order.get('closePosition', False))).lower() == 'true'
    if native in {'LONG', 'SHORT'}:
        side = native
    elif not hedged:
        side = ('LONG' if action == 'sell' else 'SHORT') if reduce or all_close else ('LONG' if action == 'buy' else 'SHORT')
    else:
        raise ValueError('External order position direction is unavailable')
    role = 'entry' if (action == 'buy') == (side == 'LONG') and not reduce and not all_close else 'exit'
    if role == 'exit' and not hedged and not (reduce or all_close):
        raise ValueError('External exit has no close-only evidence')
    return side, role, all_close


def _external_record(order, side, role, all_close):
    info = order.get('info') or {}
    kind = str(order.get('type') or info.get('type') or '').lower()
    native = str(info.get('orderType') or info.get('type') or '').lower()
    trigger = order.get('triggerPrice') or order.get('stopPrice') or info.get('triggerPrice') or info.get('stopPrice')
    if not trigger:
        trigger = order.get('stopLossPrice') or order.get('takeProfitPrice')
    aliases = {'stop': 'stop_limit', 'take_profit': 'take_profit_limit', 'stop_loss': 'stop_market'}
    kind = aliases.get(native, native) if native in aliases or native in {'stop_market', 'take_profit_market', 'stop_limit', 'take_profit_limit'} else aliases.get(kind, kind)
    if order.get('stopLossPrice') and kind in {'market', 'limit'}:
        kind = 'stop_market' if kind == 'market' else 'stop_limit'
    if order.get('takeProfitPrice') and kind in {'market', 'limit'}:
        kind = 'take_profit_market' if kind == 'market' else 'take_profit_limit'
    from backend.utils.exit_orders import ORDER_TYPES
    if kind not in ORDER_TYPES or (role == 'entry' and kind != 'limit'):
        raise ValueError('Unsupported external order combination')
    if kind not in {'market', 'limit'} and trigger is None:
        raise ValueError('External trigger price is unavailable')
    amount = None if all_close else _number(order.get('amount'))
    filled = _number(order.get('filled', 0) or 0)
    if amount is not None and (amount <= 0 or filled > amount + 1e-12):
        raise ValueError('External order quantity is invalid')
    record = dict(id=str(order['id']), client_id=order.get('clientOrderId') or f"external:{order['_channel']}:{order['id']}",
                  status=order.get('status') or 'open', amount=amount, filled=filled,
                  price=order.get('price'), trigger_price=trigger, side=side,
                  contract_version=2, order_type=kind, quantity_mode='all' if all_close else 'fixed',
                  exchange_channel=order['_channel'], source='external', created_at=float(order.get('timestamp') or time.time()*1000)/1000,
                  fill_verified=order.get('filled') is not None, fill_observed_at=time.time(),
                  waiting_trigger=order['_channel'] == 'algo', reason=order.get('reason'))
    if role == 'exit':
        record['exit_type'] = kind
    return record


def _facts(conn, service, symbol, identity, payload):
    conn.execute('INSERT INTO position_management_facts VALUES(?,?,?,?,?) ON CONFLICT(account_scope,symbol,identity) '
                 'DO UPDATE SET payload=excluded.payload',
                 (service.account_scope, symbol, identity, service.config_id, json.dumps(payload, default=str)))


def _save_observation(conn, service, symbol, side, observation):
    row = conn.execute('SELECT payload FROM position_management_scopes WHERE account_scope=? AND symbol=? AND side=? AND owner=?',
                       (service.account_scope, symbol, side, service.config_id)).fetchone()
    if row:
        payload = {**json.loads(row['payload']), **observation}
        if observation.get('sync_status') == 'verified':
            payload.pop('error', None)
        conn.execute('UPDATE position_management_scopes SET payload=? WHERE account_scope=? AND symbol=? AND side=? AND owner=?',
                     (json.dumps(payload), service.account_scope, symbol, side, service.config_id))


def ensure_managed(service, symbol, side=None):
    """Verify a complete observation and enroll locally, without exchange writes."""
    if _in_discovery.get() or _in_action.get() or not enabled(service, symbol, side):
        return
    key = (service.account_scope, canonical_symbol(symbol))
    observation_key = (*key, service.config_id, side)
    with _discovery_guard:
        lock = _discovery_locks.setdefault(key, threading.Lock())
        generation = _discovery_success.get(observation_key, 0)
    from backend.mcp.resilience import remaining
    waited = not lock.acquire(blocking=False)
    if waited and not lock.acquire(timeout=min(5, remaining('management_observation_queue'))):
        raise ValueError('Management observation is busy; refresh the current scope')
    token = _in_discovery.set(True)
    try:
        if waited and _discovery_success.get(observation_key, 0) != generation:
            return  # Share only the complete observation made while this caller waited.
        _discover(service, symbol, side)
        _discovery_success[observation_key] = generation + 1
    except Exception as exc:
        from backend.database import get_db_conn
        try:
            with get_db_conn() as conn:
                for row in conn.execute('SELECT side FROM position_management_scopes WHERE owner=? AND account_scope=? AND symbol=?',
                                        (service.config_id, service.account_scope, key[1])):
                    if side is None or row['side'] == side:
                        _save_observation(conn, service, key[1], row['side'],
                                          {'sync_status': 'partial', 'error': type(exc).__name__ + ': management observation incomplete',
                                           'attempted_at': time.time()})
                conn.commit()
        except Exception:
            pass  # Preserve the original failed observation, including DB lock errors.
        raise
    finally:
        _in_discovery.reset(token)
        lock.release()


def _discover(service, symbol, requested_side):
    from backend.database import get_db_conn
    from backend.utils.execution_ledger import ExecutionLedger, rebuild_execution_position_history
    from backend.utils.exit_orders import order_reference
    symbol = service._market(symbol)['symbol']
    mode = service.ex.fetch_position_mode(symbol)
    if not isinstance(mode, dict) or type(mode.get('hedged')) is not bool:
        raise ValueError('Position mode is unavailable')
    hedged = mode['hedged']
    config = _configuration(service)
    visible = config.get('position_management_policy') == 'visible_first_claim'
    sides = ([requested_side] if visible and requested_side else
             ['LONG', 'SHORT'] if visible else
             [s['side'] for s in normalize_management(config)['management_scopes']
              if s['symbol'] == symbol and (requested_side is None or s['side'] == requested_side)])
    if not visible:
        for side in sides:
            _claim(service, symbol, side, hedged)
    positions = service.ex.fetch_positions([symbol])
    if not isinstance(positions, list):
        raise ValueError('Position snapshot is unavailable')
    position_map = {}
    for row in positions:
        if _number(row.get('contracts')):
            if row.get('symbol') != symbol or str(row.get('side', '')).upper() not in {'LONG', 'SHORT'}:
                raise ValueError('Position snapshot scope is invalid')
            position_map[str(row['side']).upper()] = row
    orders = _orders(service.ex, symbol)
    now_ms = int(service.ex.fetch_time()) if hasattr(service.ex, 'fetch_time') else int(time.time()*1000)
    for side in sides:
        current = service._load(symbol, side)
        plan = current if current and current.get('state') != 'DONE' else None
        position = position_map.get(side)
        quantity = _number((position or {}).get('contracts', 0))
        relevant_orders = []
        for order in orders.values():
            try:
                if _order_side(order, hedged)[0] == side:
                    relevant_orders.append(order)
            except ValueError:
                continue
        if plan is None and not quantity and not relevant_orders:
            with get_db_conn() as conn:
                _save_observation(conn, service, symbol, side,
                                  {'policy': 'exclusive_scope', 'sync_status': 'verified', 'contracts': 0,
                                   'hedged': hedged, 'observed_at': time.time()})
                conn.commit()
            continue
        if visible:
            from backend.utils.visible_management import claim_management
            current_claim = claim_management(service.mt, symbol, side)
            _claim(service, symbol, side, hedged)
        if plan is None and quantity:
            from backend.utils.independent_cycle_recovery import restore_cycle
            restored, _ = restore_cycle(service, symbol, side, current)
            plan = restored
        fresh = plan is None
        plan = plan or service._new(symbol, side)
        if not plan.get('management'):
            baseline_at = now_ms if fresh else int(min([e.get('created_at', now_ms/1000) for e in plan.get('entries', [])] or [now_ms/1000])*1000)
            baseline = quantity if fresh else float((plan.get('adoption') or {}).get('contracts') or 0)
            plan['management'] = {'policy': 'exclusive_scope', 'source': 'baseline' if fresh and quantity else 'agent',
                                  'baseline_contracts': baseline, 'baseline_at_ms': baseline_at,
                                  'owner': service.config_id, 'sync_status': 'pending', 'observed_at': None}
        management = plan['management']
        if visible:
            management.update(policy='visible_first_claim', claim_id=current_claim['claim_id'],
                              claim_version=current_claim['version'], owner=service.config_id)
        # Resolve existing parent/client identities before indexing fills or
        # classifying a newly visible execution child as an external order.
        for record in plan.get('entries', []) + plan.get('exits', []):
            if record.get('status') in {'closed', 'canceled', 'cancelled', 'expired', 'rejected'} and not record.get('fill_pending'):
                continue
            try:
                if service._query(record, symbol) is None:
                    record.update(fill_pending=True, query_error='Current order identity requires verification')
            except Exception as exc:
                record.update(fill_pending=True, query_error=type(exc).__name__ + ': order observation incomplete')
        ledger = ExecutionLedger(service.ex, service.config_id, symbol)
        sync = ledger.sync(now_ms=now_ms, start_ms=max(0, management['baseline_at_ms']-60000), min_interval=0, max_calls=20)
        if not sync.get('complete'):
            management.update(sync_status='partial', error=sync.get('error') or 'Fill coverage is incomplete')
            plan['ownership_error'] = management['error']
            service._save(plan)
            raise ValueError(management['error'])
        with get_db_conn() as conn:
            fills = [dict(r) for r in conn.execute('SELECT * FROM execution_fills WHERE account_scope=? AND symbol=? '
                                                  'AND timestamp_ms>=? ORDER BY timestamp_ms,trade_id',
                                                  (service.account_scope, symbol, management['baseline_at_ms']))]
            links = {str(r['order_id']): dict(r) for r in conn.execute('SELECT * FROM execution_order_links '
                     'WHERE account_scope=? AND symbol=?', (service.account_scope, symbol))}
        for record in plan.get('entries', []) + plan.get('exits', []):
            reference = order_reference(record)
            if reference and reference.startswith('regular:'):
                links.setdefault(reference.split(':', 1)[1],
                                 {'config_id': service.config_id, 'payload': json.dumps({
                                     'side': side, 'source': record.get('source', 'agent'),
                                     'episode_id': plan['episode_id']})})
        amount = float(management['baseline_contracts'])
        seen = set(management.get('baseline_trade_ids', []))
        fills.sort(key=lambda f: (f['timestamp_ms'], (0, int(f['trade_id'])) if str(f['trade_id']).isdigit() else (1, str(f['trade_id']))))
        ambiguous_times = set()
        time_groups = {}
        for fill in fills:
            if fill['trade_id'] not in seen:
                time_groups.setdefault(fill['timestamp_ms'], []).append(fill)
        for timestamp, peers in time_groups.items():
            if len(peers) > 1 and not all(str(f['trade_id']).isdigit() for f in peers):
                actions = {json.loads(f['payload']).get('side') for f in peers}
                if len(actions) > 1:
                    ambiguous_times.add(timestamp)
        observed_fills = []
        boundary = None
        reached_flat = False
        for fill in fills:
            payload = json.loads(fill['payload'])
            trade_side = str(payload.get('position_side') or '').upper()
            link = links.get(str(fill['order_id']))
            if link:
                metadata = json.loads(link['payload'])
                trade_side = metadata.get('side') or trade_side
                if link['config_id'] != service.config_id and link['config_id'] != plan.get('original_owner') and (not hedged or trade_side == side):
                    raise ValueError('A fill belongs to another active manager')
            if trade_side not in {'LONG', 'SHORT'}:
                if hedged:
                    raise ValueError('External fill position direction is unavailable')
                trade_side = side
            if trade_side != side or fill['trade_id'] in seen:
                continue
            contracts = _number(payload.get('amount'))
            action = str(payload.get('side') or '').lower()
            if action not in {'buy', 'sell'}:
                raise ValueError('External fill direction is unavailable')
            role = 'entry' if (action == 'buy') == (side == 'LONG') else 'agent_exit'
            if not fresh:
                if reached_flat and role == 'entry' and fill['timestamp_ms'] not in ambiguous_times:
                    boundary = len(observed_fills)
                amount += contracts if role == 'entry' else -contracts
                if amount < -1e-10 and fill['timestamp_ms'] not in ambiguous_times:
                    raise ValueError('Fill sequence crosses the configured position direction')
                reached_flat = abs(amount) < 1e-10
            else:
                seen.add(fill['trade_id'])
            observed_fills.append((fill, payload, role, link))
        if ambiguous_times and not fresh:
            management['boundary_pending'] = True
            management['boundary_reason'] = 'Same-time external fills have no provable exchange sequence'
            boundary = None
        if fresh:
            management['baseline_trade_ids'] = sorted(seen)
        if abs(amount-quantity) > max(1e-10, quantity*1e-8):
            management.update(sync_status='partial', error='Position changes do not match verified fills')
            plan['ownership_error'] = management['error']
            service._save(plan)
            raise ValueError(management['error'])
        old_episode = plan['episode_id']
        if boundary is not None:
            boundary_time = observed_fills[boundary][0]['timestamp_ms']
            # Keep the closed episode in audit history. Its remaining orders are
            # a separate cleanup queue, never successor entries or protection.
            old_plan = json.loads(json.dumps(plan))
            old_plan.update(state='DONE', closed_at=boundary_time / 1000,
                            close_source='external_verified_fills')
            service._save(old_plan)
            plan = service._new(symbol, side)
            old_refs = {order_reference(r) for r in old_plan['entries'] + old_plan['exits']}
            pending = list(management.get('pending_orders', [])) + [r for r in old_plan['entries'] + old_plan['exits']
                       if r.get('status') not in {'closed', 'canceled', 'cancelled', 'expired', 'rejected'}
                       or r.get('fill_pending')]
            pending = list({order_reference(r) or 'client:' + str(r.get('client_id')): r for r in pending}.values())
            old_refs.update(order_reference(r) for r in pending)
            for identity, order in orders.items():
                if identity in old_refs or order.get('timestamp') is None or order['timestamp'] >= boundary_time:
                    continue
                try:
                    order_side, role, all_close = _order_side(order, hedged)
                    if order_side == side:
                        pending.append(_external_record(order, side, role, all_close))
                        old_refs.add(identity)
                except ValueError:
                    continue
            management = {**management, 'baseline_contracts': 0, 'baseline_at_ms': boundary_time,
                          'baseline_trade_ids': [f['trade_id'] for f, _, _, _ in observed_fills[:boundary]
                                                 if f['timestamp_ms'] == boundary_time],
                          'source': 'external', 'previous_episode_id': old_episode,
                          'pending_orders': pending}
            plan['management'] = management
        else:
            old_refs = set()
        # Batch new facts/links in one short local transaction. Existing source
        # and episode associations are immutable when an observation repeats.
        with get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for index, (fill, payload, role, link) in enumerate(observed_fills):
                source = (json.loads(link['payload']).get('source') or 'agent') if link else 'external'
                episode = old_episode if boundary is not None and index < boundary else plan['episode_id']
                metadata = {'side': side, 'episode_id': episode, 'source': source}
                existing_link = conn.execute('SELECT config_id FROM execution_order_links WHERE account_scope=? AND symbol=? AND order_id=?',
                                            (service.account_scope, symbol, str(fill['order_id']))).fetchone()
                if existing_link and existing_link['config_id'] not in {service.config_id, plan.get('original_owner')}:
                    raise ValueError('A fill belongs to another active manager')
                if not existing_link:
                    conn.execute('INSERT INTO execution_order_links VALUES(?,?,?,?,?,?)',
                                 (service.account_scope, symbol, str(fill['order_id']), service.config_id, role, json.dumps(metadata)))
                    conn.execute('UPDATE execution_fills SET config_id=? WHERE account_scope=? AND symbol=? AND order_id=?',
                                 (service.config_id, service.account_scope, symbol, str(fill['order_id'])))
                _facts(conn, service, symbol, 'trade:' + fill['trade_id'], {**payload, 'source': source})
            conn.commit()
        rebuild_execution_position_history(service.config_id, service.account_scope, symbol)
        management['unsupported_orders'] = {}
        for identity, order in orders.items():
            if identity in old_refs or any(order_reference(r) == identity for r in management.get('pending_orders', [])):
                continue
            try:
                order_side, role, all_close = _order_side(order, hedged)
                if order_side != side:
                    continue
                existing = next((r for r in plan.get('entries', []) + plan.get('exits', []) if order_reference(r) == identity
                                 or r.get('client_id') == order.get('clientOrderId') and order.get('clientOrderId')), None)
                if existing:
                    observed = _external_record(order, side, role, all_close)
                    fields = ('price',) if existing.get('execution_order_id') else ('price', 'trigger_price', 'amount', 'quantity_mode', 'order_type', 'exit_type')
                    for field in fields:
                        if field in observed and existing.get(field) != observed[field]:
                            existing[field] = observed[field]
                            existing['last_change_source'] = 'external'
                    with get_db_conn() as conn:
                        _facts(conn, service, symbol, 'order:' + identity,
                               {**order, 'source': existing.get('source', 'agent'), 'observed_at': time.time()})
                        conn.commit()
                    continue
                linked = links.get(str(order['id']) if order['_channel'] == 'regular' else identity)
                if linked and linked['config_id'] not in {service.config_id, plan.get('original_owner')}:
                    raise ValueError('External order belongs to another manager')
                record = _external_record(order, side, role, all_close)
                if linked:
                    previous_metadata = json.loads(linked['payload'])
                    record['source'] = previous_metadata.get('source', 'agent')
                    record['restored_from_episode_id'] = previous_metadata.get('episode_id')
                plan['entries' if role == 'entry' else 'exits'].append(record)
            except ValueError as exc:
                if 'another manager' in str(exc):
                    raise
                management.setdefault('unsupported_orders', {})[identity] = str(exc)
            with get_db_conn() as conn:
                _facts(conn, service, symbol, 'order:' + identity, {**order, 'source': (record.get('source', 'external') if identity not in management['unsupported_orders'] else 'external'), 'observed_at': time.time()})
                conn.commit()
        for record in plan['entries'] + plan['exits']:
            if record.get('status') not in {'closed', 'canceled', 'cancelled', 'expired', 'rejected'} and order_reference(record) not in orders:
                try:
                    if service._query(record, symbol) is None:
                        record.update(fill_pending=True, query_error='Order missing from exchange lists; final outcome requires verification')
                except Exception as exc:
                    record.update(fill_pending=True, query_error=str(exc))
        confirmed = service._position(plan)
        if _number((confirmed or {}).get('contracts', 0)) != quantity:
            raise ValueError('Position changed during enrollment; refresh before trading')
        if quantity:
            plan.update(state='ACTIVE', ever_filled=True)
        management.update(sync_status='verified', observed_at=time.time(), contracts=quantity, hedged=hedged)
        management.pop('error', None)
        plan.pop('ownership_error', None)
        plan['contract_size'] = float(service._market(symbol).get('contractSize') or 1)
        plan['position_observation'] = {'status': 'verified', 'amount': quantity * plan['contract_size'],
                                        'observed_at': management['observed_at']}
        if quantity or plan['entries'] or plan['exits'] or current:
            service._save(plan)
        with get_db_conn() as conn:
            _save_observation(conn, service, symbol, side, management)
            conn.commit()


def management_snapshot(config_id, symbol=None):
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        scopes = [{**json.loads(r['payload']), 'symbol': r['symbol'], 'side': r['side'], 'owner': r['owner']}
                  for r in conn.execute('SELECT * FROM position_management_scopes WHERE owner=?', (config_id,))
                  if not symbol or canonical_symbol(symbol) == r['symbol']]
    return {'policy': 'exclusive_scope' if scopes else 'strict_owned', 'scopes': scopes}


def cleanup_previous_orders(service, symbol):
    """Cancel only durable predecessor targets; uncertainty retains the scope."""
    from backend.utils.exit_orders import order_reference
    from backend.utils.trade_order_evidence import classify_saved_order
    for side in ('LONG', 'SHORT'):
        if not enabled(service, symbol, side):
            continue
        plan = service._load(symbol, side)
        management = (plan or {}).get('management', {})
        pending = management.get('pending_orders', [])
        if not pending:
            continue
        # A predecessor's lost ACK may retain only its original client ID.
        # Resolve that identity by reading, never by creating another order.
        resolved_identity = False
        for record in pending:
            if not record.get('cleanup_identity') and not order_reference(record, current=False):
                if service._query(record, symbol) is None:
                    raise ValueError('Predecessor cleanup has an unverifiable original client identity')
                resolved_identity = True
        if resolved_identity:
            service._save(plan)
        targets = sorted(record.get('cleanup_identity') or order_reference(record, current=False)
                         for record in pending if record.get('cleanup_identity') or order_reference(record, current=False))
        if len(targets) != len(pending):
            raise ValueError('Predecessor cleanup has an unverifiable order identity')
        operation_id = 'maintenance:' + plan['episode_id'] + ':' + hashlib.sha256(json.dumps(targets).encode()).hexdigest()[:16]
        with action_reservation(service, symbol, side, operation_id):
            for record in pending:
                record.setdefault('cleanup_identity', order_reference(record, current=False))
            management['cleanup_operation'] = {'operation_id': operation_id, 'episode_id': plan['episode_id'],
                                                'targets': targets, 'status': 'pending'}
            service._save(plan)
            current_targets = {order_reference(record) for record in plan.get('entries', []) + plan.get('exits', [])}
            for record in list(pending):
                try:
                    if service._query(record, symbol) is None:
                        raise ValueError('Predecessor order outcome is unavailable')
                    if order_reference(record) in current_targets or record['cleanup_identity'] in current_targets:
                        raise ValueError('Predecessor target is present in the successor cycle; cleanup requires identity verification')
                    record['cancel_operation_id'] = operation_id
                    # If the parent triggers after this observation, stop and
                    # re-check the child identity on the next pass before any
                    # child cancellation; it may now be a successor target.
                    service._cancel(record, plan, allow_child=bool(record.get('execution_order_id')))
                    if classify_saved_order(record)['state'] in {'terminal_verified', 'not_accepted'}:
                        pending.remove(record)
                    record.pop('cleanup_error', None)
                except Exception as exc:
                    record['cleanup_error'] = str(exc)
            management['cleanup_operation']['status'] = 'pending' if pending else 'completed'
            # CAS rejects a concurrent successor update; it must never be
            # overwritten by this earlier cleanup observation.
            service._save(plan)
            if pending:
                raise RuntimeError('Predecessor cleanup remains unresolved; original reservation retained')


def recover_reservations(service, symbol):
    """Observe original identities, never replay writes or release by lease age."""
    if _in_action.get():
        return 0
    from backend.database import get_db_conn
    from backend.utils.management_lock import scope_file_lock, ManagementLockBusy
    from backend.utils.trade_operations import _decode_receipt, _same_symbol, tool_result_status
    from backend.utils.trade_recovery import operation_evidence, recover_operation
    from backend.utils.trade_order_evidence import classify_saved_order
    from backend.utils.exit_orders import order_reference
    with get_db_conn() as conn:
        rows = list(conn.execute("SELECT * FROM position_management_actions WHERE owner=? AND account_scope=? AND symbol=? "
                                 "AND state IN ('running','unresolved')", (service.config_id, service.account_scope, symbol)))
        receipts = list(conn.execute('SELECT * FROM trade_action_runs WHERE config_id=?', (service.config_id,)))
    released = 0
    for row in rows:
        try:
            with scope_file_lock(service.account_scope, symbol, row['side']):
                identity = row['operation_id']
                proven = False
                if identity.startswith('maintenance:'):
                    # Cleanup has no Agent callback receipt. Its persisted target set
                    # and individually verified terminal order facts are the evidence.
                    sides = ('LONG', 'SHORT') if row['side'] == 'BOTH' else (row['side'],)
                    for side in sides:
                        plan = service._load(symbol, side)
                        management = (plan or {}).get('management', {})
                        intent = management.get('cleanup_operation', {})
                        if (intent.get('operation_id') != identity or not intent.get('episode_id')
                                or not identity.startswith('maintenance:' + intent['episode_id'] + ':')):
                            continue
                        pending = management.get('pending_orders', [])
                        if intent.get('status') == 'completed' and not pending:
                            proven = True
                            break
                        current = {order_reference(record) for record in plan.get('entries', []) + plan.get('exits', [])}
                        targets = set(intent.get('targets', []))
                        if not targets or current & targets:
                            continue
                        records = {record.get('cleanup_identity') or order_reference(record, current=False): record for record in pending}
                        if not targets <= records.keys():
                            continue
                        try:
                            for target in targets:
                                record = records[target]
                                if classify_saved_order(record)['state'] not in {'terminal_verified', 'not_accepted'}:
                                    service._query(record, symbol)
                            if any(order_reference(records[target]) in current for target in targets):
                                continue
                            proven = all(classify_saved_order(records[target])['state'] in {'terminal_verified', 'not_accepted'}
                                         for target in targets)
                            if proven:
                                management['pending_orders'] = [record for record in pending
                                                                if (record.get('cleanup_identity') or order_reference(record, current=False)) not in targets]
                                intent['status'] = 'completed'
                            elif (row['state'] == 'unresolved'
                                  and all(not records[target].get('cancel_requested_at') for target in targets)):
                                # _cancel persists this marker before sending. The
                                # failed worker has exited; these targets were never
                                # sent, so retrying the same cleanup intent is safe.
                                # A still-running worker is never released this way.
                                intent['status'] = 'not_submitted'
                                proven = True
                            service._save(plan)
                        except Exception:
                            proven = False
                        break
                else:
                    candidates = sorted((receipt for receipt in receipts if _same_symbol(receipt['symbol'], symbol)
                                         and (receipt['operation_id'] == identity or identity.startswith(receipt['operation_id'] + ':'))),
                                        key=lambda receipt: len(receipt['operation_id']), reverse=True)
                    if candidates:
                        parent = candidates[0]
                        try:
                            # Network observations occur outside the SQLite transaction.
                            recovered = recover_operation(service.config_id, symbol, parent['operation_id'], service=service)
                            evidence = operation_evidence(service.config_id, symbol, [identity], account_scope=service.account_scope)[identity]
                            latest = evidence.get('latest', {})
                            proven = evidence.get('resolution') == 'matched' and tool_result_status(latest) in {
                                'submitted', 'completed', 'skipped', 'failed'}
                            if not proven and parent['operation_id'] == identity:
                                receipt = _decode_receipt(parent['result'])
                                proven = (isinstance(receipt, dict) and receipt.get('status') in {'failed', 'skipped'}
                                          and receipt.get('execution', {}).get('submission_state') == 'not_submitted')
                            # Synchronize the original outer MCP identity as well, so
                            # configuration retirement cannot stall on an old unknown.
                            if (service.config_id.startswith('mcp:') and isinstance(recovered, dict)
                                    and recovered.get('error_code') not in {'operation_not_found', 'account_scope_mismatch'}):
                                from backend.mcp import store
                                from backend.mcp.service import _external_operation_receipt
                                with get_db_conn() as conn:
                                    links = list(conn.execute('SELECT * FROM mcp_operation_links WHERE profile_id=? AND internal_operation_id=?',
                                                              (service.config_id[4:], parent['operation_id'])))
                                for link in links:
                                    result = {'operation_id': link['operation_id'], 'status': tool_result_status(recovered),
                                              'result': _external_operation_receipt(recovered, parent['operation_id'], link['operation_id'],
                                                                                   service.config_id[4:], principal=link['principal'])}
                                    store.complete_operation(service.config_id[4:], link['operation_id'], result)
                        except Exception:
                            proven = False
                if proven:
                    with get_db_conn() as conn:
                        changed = conn.execute("UPDATE position_management_actions SET state='finished',updated_at=? "
                                               "WHERE owner=? AND account_scope=? AND symbol=? AND side=? AND operation_id=? AND state=?",
                                               (time.time(), service.config_id, service.account_scope, symbol, row['side'], identity, row['state']))
                        released += changed.rowcount
                        conn.commit()
        except ManagementLockBusy:
            # The ACK may be visible while its callback still has more work.
            # Only the original live worker may finish/release that scope.
            continue
    return released


@contextmanager
def action_reservation(service, symbol, side, operation_id):
    from backend.database import get_db_conn
    from backend.utils.management_lock import scope_file_lock
    active = _in_action.get()
    if active:
        if active['account_scope'] != service.account_scope or active['symbol'] != symbol or active['side'] not in {side, 'BOTH'}:
            raise ValueError('Nested exchange action crosses its reserved management scope')
        yield
        return
    if not enabled(service, symbol, side):
        yield
        return
    mode = service.ex.fetch_position_mode(symbol)
    if not isinstance(mode, dict) or type(mode.get('hedged')) is not bool:
        raise ValueError('Position mode is unavailable for financial coordination')
    scope_side = side if mode['hedged'] else 'BOTH'
    with scope_file_lock(service.account_scope, symbol, scope_side):
        with get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute("SELECT * FROM position_management_actions WHERE account_scope=? AND symbol=? "
                               "AND (side=? OR side='BOTH' OR ?='BOTH') AND state IN ('running','unresolved') LIMIT 1",
                               (service.account_scope, symbol, scope_side, scope_side)).fetchone()
            if old:
                raise ValueError('Another exchange action is running; inspect its original operation')
            conn.execute('INSERT INTO position_management_actions VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_scope,symbol,side) '
                         'DO UPDATE SET owner=excluded.owner,operation_id=excluded.operation_id,state=excluded.state,updated_at=excluded.updated_at',
                         (service.account_scope, symbol, scope_side, service.config_id, operation_id, 'running', time.time()))
            conn.commit()
        token = _in_action.set({'operation_id': operation_id, 'account_scope': service.account_scope,
                                'symbol': symbol, 'side': scope_side})
        state = 'unresolved'
        completion = {}
        try:
            yield completion
            state = 'unresolved' if completion.get('unknown') else 'finished'
        finally:
            from backend.utils.trade_receipts import current_attempt
            attempt = current_attempt.get()
            if state == 'unresolved' and attempt is not None and attempt.submission_state == 'not_submitted':
                state = 'finished'
            _in_action.reset(token)
            with get_db_conn() as conn:
                conn.execute('UPDATE position_management_actions SET state=?,updated_at=? WHERE account_scope=? AND symbol=? '
                             'AND side=? AND operation_id=?', (state, time.time(), service.account_scope, symbol, scope_side, operation_id))
                conn.commit()


def managed_action(function):
    @wraps(function)
    def wrapped(service, symbol, *args, **kwargs):
        if function.__name__ == 'open':
            from backend.mcp.guard import assert_entry_allowed
            assert_entry_allowed(service.mt, symbol)
        if not enabled(service, symbol):
            return function(service, symbol, *args, **kwargs)
        name = function.__name__
        from backend.utils.trade_operations import current_operation_id, tool_result_status
        from backend.utils.visible_management import _active_management
        operation_id = kwargs.get('operation_id') or current_operation_id.get() or 'local:' + hashlib.sha256(str(time.time_ns()).encode()).hexdigest()
        if _configuration(service).get('position_management_policy') == 'visible_first_claim':
            active = _active_management.get()
            if active:
                result = function(service, symbol, *args, **kwargs)
                active['outcome_status'] = tool_result_status(result)
                return result
            target = None
            if name == 'open':
                op = args[0] if args else kwargs['op']
                side = 'LONG' if op.action == 'BUY_LIMIT' else 'SHORT'
            elif name == 'close':
                side = args[0] if args else kwargs['pos_side']
            else:
                side = None
                target = args[0] if args else kwargs['order_id']
            with management_action(service.mt, symbol, side, operation_id, automatic=False, order_id=target) as claim:
                result = function(service, symbol, *args, **kwargs)
                if claim:
                    claim['outcome_status'] = tool_result_status(result)
                return result
        ensure_managed(service, symbol)
        recover_reservations(service, service._market(symbol)['symbol'])
        if name == 'open':
            op = args[0] if args else kwargs['op']
            side = 'LONG' if op.action == 'BUY_LIMIT' else 'SHORT'
        elif name == 'close':
            side = args[0] if args else kwargs['pos_side']
        else:
            order_id = args[0] if args else kwargs['order_id']
            plan, _ = service._find_order(symbol, order_id)
            side = plan['side']
        with action_reservation(service, service._market(symbol)['symbol'], side, operation_id) as completion:
            result = function(service, symbol, *args, **kwargs)
            active = _active_management.get()
            if active:
                active['outcome_status'] = tool_result_status(result)
            if completion is not None:
                completion['unknown'] = tool_result_status(result) == 'unknown'
            return result
    return wrapped


# Public management APIs deliberately share one implementation for the UI,
# financial adapters and background discovery. Status/recovery are read-only.
from backend.utils.visible_management import (
    management_status, release_management_scope, transfer_management_scope,
    management_recovery_targets, lookup_management_recovery, query_management_recovery,
    assert_configuration_claims, physical_account_scope,
    resource_management_status,
    reconcile_visible_actions,
)


def _ensure_visible_management(market_tool, symbol, side=None, *, operation_id=None, discover=False):
    from backend.utils.visible_management import claim_management, _active_management
    cfg = dict(getattr(market_tool, 'runtime_config', {}) or {})
    if str(cfg.get('mode', '')).upper() == 'STRATEGY':
        return None
    existing_scopes = management_status(market_tool.config_id, symbol)['scopes']
    if discover and (cfg.get('read_only') or cfg.get('enabled') is False and not existing_scopes):
        return None
    if getattr(market_tool, 'market_type', cfg.get('market_type')) == 'spot':
        if discover:
            balance = market_tool.exchange.fetch_balance()
            base = symbol.split('/')[0]
            quantity = (balance.get(base) or {}).get('total', (balance.get('total') or {}).get(base, 0))
            if not quantity and not market_tool.exchange.fetch_open_orders(symbol):
                return None
        claim = claim_management(market_tool, symbol, 'BASE', operation_id)
        from backend.mcp.spot_orders import _visible_inventory
        _visible_inventory(market_tool, symbol, claim=claim)
        return claim
    if side is not None:
        claim = claim_management(market_tool, symbol, side, operation_id)
    else:
        positions = market_tool.exchange.fetch_positions([symbol])
        orders = _orders(market_tool.exchange, symbol)
        hedged = bool(market_tool.exchange.fetch_position_mode(symbol).get('hedged'))
        sides = {str(p.get('side', '')).upper() for p in positions if float(p.get('contracts') or 0)}
        for order in orders.values():
            try:
                sides.add(_order_side(order, hedged)[0])
            except ValueError:
                continue
        if not sides:
            return None
        claims = [claim_management(market_tool, symbol, target, operation_id) for target in sorted(sides)]
        claim = claims[0]
    # Existing independent enrollment supplies current execution evidence and
    # normal cycle recovery without changing original trade origin.
    from backend.utils.independent_exits import IndependentExits
    service = IndependentExits(market_tool)
    previous = market_tool.runtime_config
    market_tool.runtime_config = {**cfg, 'position_management_policy': 'visible_first_claim'}
    try:
        ensure_managed(service, symbol, side)
    finally:
        market_tool.runtime_config = previous
    return claim


def ensure_visible_management(market_tool, symbol, side=None, *, operation_id=None, discover=False):
    previous = getattr(market_tool, 'runtime_config', {}) or {}
    if discover and previous.get('enabled') is False:
        market_tool.runtime_config = {**previous, '_management_maintenance': True}
    try:
        return _ensure_visible_management(market_tool, symbol, side, operation_id=operation_id, discover=discover)
    finally:
        market_tool.runtime_config = previous


def discover_management_symbols(market_tool, config):
    """Bounded actual resource discovery; never enumerates all market symbols."""
    selected = config.get('symbols') or ([config['symbol']] if config.get('symbol') else [])
    owned = [s['symbol'] for s in management_status(market_tool.config_id)['scopes']]
    if config.get('mcp_symbol_scope', config.get('symbol_scope')) != 'all':
        return list(dict.fromkeys([*selected, *owned]))
    discovered = list(owned)
    if config.get('enabled', True):
        if getattr(market_tool, 'market_type', None) == 'spot':
            balances = market_tool.exchange.fetch_balance()
            totals = balances.get('total') or {base: values.get('total', 0) for base, values in balances.items() if isinstance(values, dict)}
            allowed = getattr(market_tool.exchange, 'markets', {})
            for base, quantity in totals.items():
                if quantity:
                    target = next((s for s, m in allowed.items() if m.get('spot') and m.get('base') == base), None)
                    if target:
                        discovered.append(target)
        else:
            discovered.extend(p['symbol'] for p in market_tool.exchange.fetch_positions() if float(p.get('contracts') or 0))
        discovered.extend(o['symbol'] for o in market_tool.exchange.fetch_open_orders() if o.get('symbol'))
    return list(dict.fromkeys(discovered))


@contextmanager
def management_action(market_tool, symbol, side, operation_id, *, automatic=True, order_id=None):
    from backend.utils.visible_management import financial_reservation, _active_management
    cfg = getattr(market_tool, 'runtime_config', {}) or {}
    if str(cfg.get('mode', '')).upper() == 'STRATEGY':
        yield None
        return
    previous = market_tool.runtime_config
    if automatic:
        market_tool.runtime_config = {**cfg, 'position_management_policy': 'visible_first_claim', 'management_scopes': []}
    try:
        if side is None and order_id and getattr(market_tool, 'market_type', cfg.get('market_type')) != 'spot':
            raw = str(order_id)
            channel, separator, identity = raw.partition(':')
            params = {'trigger': True} if separator and channel == 'algo' else {}
            order = market_tool.exchange.fetch_order(identity if separator else raw, symbol, params=params)
            requested = identity if separator else raw
            info = order.get('info') or {}
            actual_id = info.get('algoId', order.get('id')) if channel == 'algo' else order.get('id')
            market = market_tool.exchange.market(symbol)
            observed_symbol = order.get('symbol')
            if (str(actual_id) != requested or not observed_symbol
                    or observed_symbol.split(':')[0] != market['symbol'].split(':')[0]
                    or separator and channel not in {'regular','algo'}
                    or channel == 'regular' and order.get('_channel') == 'algo'):
                raise ValueError('Exact order identity and market must be verified before claiming management')
            mode = market_tool.exchange.fetch_position_mode(symbol)
            side = _order_side({**order, '_channel': channel if separator else 'regular'}, bool(mode['hedged']))[0]
        claim = ensure_visible_management(market_tool, symbol, side, operation_id=operation_id)
        if claim is None:
            yield None
            return
        if not _active_management.get():
            reconcile_visible_actions(market_tool.config_id)
        with financial_reservation(market_tool, claim, operation_id):
            # Reuse the original independent reservation context so its nested
            # tools do not try to acquire an unrelated file lock again.
            token = _in_action.set({'operation_id': operation_id, 'account_scope': _legacy_scope_for_tool(market_tool),
                                    'symbol': symbol, 'side': claim['side']})
            try:
                yield claim
            finally:
                _in_action.reset(token)
    finally:
        market_tool.runtime_config = previous


def _legacy_scope_for_tool(market_tool):
    from backend.utils.execution_ledger import account_scope
    return account_scope(market_tool.exchange, market_tool.config_id)
