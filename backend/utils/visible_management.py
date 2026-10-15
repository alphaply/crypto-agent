"""Persistent management authority, independent of trade origin and cycles.

Reads never claim. Callers establish authority only after financial preflight or
from the writable configuration's background discovery. Exchange observations
remain outside SQLite transactions and original execution identities are retained.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import time

from backend import database
from backend.utils.management_lock import scope_file_lock

_active_management = ContextVar('visible_management_action', default=None)


class ManagementConflict(ValueError):
    def __init__(self, message, claim=None):
        super().__init__(message)
        self.code = 'management_scope_conflict'
        self.details = {'claim': claim or {}, 'submitted': False}


def initialize_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS management_account_aliases (
        legacy_scope TEXT PRIMARY KEY, physical_account TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS management_execution_scopes (
        physical_account TEXT NOT NULL, market_type TEXT NOT NULL,
        legacy_scope TEXT NOT NULL, PRIMARY KEY(physical_account,market_type))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS visible_management_claims (
        claim_id TEXT PRIMARY KEY, physical_account TEXT NOT NULL,
        market_type TEXT NOT NULL, resource TEXT NOT NULL, side TEXT NOT NULL,
        owner TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL,
        payload TEXT NOT NULL, UNIQUE(physical_account,market_type,resource,side))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS management_resource_links (
        claim_id TEXT NOT NULL, identity TEXT NOT NULL, original_owner TEXT,
        payload TEXT NOT NULL, PRIMARY KEY(claim_id,identity))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS management_recovery_grants (
        recovery_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, receiver TEXT NOT NULL,
        source_owner TEXT NOT NULL, operation_id TEXT NOT NULL, payload TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS visible_management_actions (
        claim_id TEXT PRIMARY KEY, owner TEXT NOT NULL, version INTEGER NOT NULL,
        operation_id TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS management_scope_retirements (
        claim_id TEXT NOT NULL, owner TEXT NOT NULL, version INTEGER NOT NULL,
        market_type TEXT NOT NULL, resource TEXT NOT NULL, symbol TEXT NOT NULL,
        reason TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(claim_id,owner,version))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS spot_management_intents (
        config_id TEXT NOT NULL, operation_id TEXT NOT NULL, symbol TEXT NOT NULL,
        target_id TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(config_id,operation_id))''')


def _decode(row):
    if row is None:
        return None
    result = dict(row)
    result.update(json.loads(result.pop('payload', '{}')))
    return result


def _legacy_resource_bound(conn, owner, legacy, symbol, side, evidence):
    """Distinguish an unused configuration reservation from effective authority."""
    from backend.utils.position_management import _scope_active
    witness = {'owner': owner, 'account_scope': legacy, 'symbol': symbol, 'side': side}
    if _scope_active(conn, witness):
        return True
    management = evidence.get('management') or evidence
    if (management.get('source') == 'baseline' or float(management.get('baseline_contracts') or 0)
            or float(management.get('contracts') or 0) or evidence.get('ever_filled')
            or evidence.get('adoption') or evidence.get('entries') or evidence.get('exits')
            or evidence.get('legs') or management.get('pending_orders')):
        return True
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'real_protection_plans' in tables:
        for row in conn.execute('SELECT payload FROM real_protection_plans WHERE config_id=?', (owner,)):
            plan = json.loads(row[0])
            if (plan.get('account_scope') == legacy and plan.get('symbol', '').split(':')[0] == symbol.split(':')[0]
                    and (side == 'BOTH' or plan.get('side') == side)):
                data = plan.get('management') or {}
                if (plan.get('ever_filled') or plan.get('adoption') or plan.get('entries') or plan.get('exits')
                        or plan.get('legs') or data.get('source') == 'baseline' or data.get('pending_orders')):
                    return True
    if 'position_management_facts' in tables:
        if conn.execute("SELECT 1 FROM position_management_facts WHERE owner=? AND account_scope=? AND symbol=? "
                        "AND identity LIKE 'order:%' LIMIT 1", (owner, legacy, symbol)).fetchone():
            return True
    return False


def _bind_account_alias(conn, legacy, identity):
    existing = conn.execute('SELECT physical_account FROM management_account_aliases WHERE legacy_scope=?', (legacy,)).fetchone()
    if existing and existing[0] != identity:
        if (existing[0].startswith('physical:') and identity.startswith('physical:')
                or conn.execute("SELECT 1 FROM visible_management_claims WHERE physical_account=? AND state='owned' LIMIT 1",
                                (existing[0],)).fetchone()):
            raise ValueError('Saved physical account identity conflicts with existing management; release or transfer before changing it')
    conn.execute('INSERT INTO management_account_aliases VALUES(?,?) ON CONFLICT(legacy_scope) '
                 'DO UPDATE SET physical_account=excluded.physical_account', (legacy, identity))


def migrate_existing_claims(conn, snapshot):
    """Idempotent local migration of existing ownership; never claim terminal history."""
    initialize_schema(conn)
    accounts = {a['profile_id']: a for a in snapshot.get('exchange_profiles') or []}
    configs = {c.get('config_id'): c for c in snapshot.get('agents') or []}
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'mcp_records' in tables:
        for row in conn.execute("SELECT payload FROM mcp_records WHERE kind='profile'"):
            c = json.loads(row[0])
            configs['mcp:' + c['profile_id']] = c
    physical = {}
    for owner, cfg in configs.items():
        account = accounts.get(cfg.get('exchange_profile_id'), cfg)
        venue = str(account.get('exchange', cfg.get('exchange', 'binance'))).replace('binanceusdm', 'binance')
        key = account.get('api_key') or cfg.get(venue + '_api_key') or cfg.get('api_key') or snapshot.get('global_' + venue + '_api_key')
        if not key:
            continue
        original_venue = 'binanceusdm' if venue == 'binance' and cfg.get('mode') != 'SPOT_DCA' and cfg.get('market_type', account.get('market_type', 'swap')) != 'spot' else venue
        legacy = hashlib.sha256(f'{original_venue}:{key}'.encode()).hexdigest()
        explicit = account.get('physical_account_id') or cfg.get('physical_account_id')
        ident = f'physical:{venue}:{explicit}' if explicit else 'credentials:' + hashlib.sha256(f'{venue}:{key}'.encode()).hexdigest()
        _bind_account_alias(conn, legacy, ident)
        kind = cfg.get('market_type', account.get('market_type', 'swap'))
        conn.execute('INSERT OR IGNORE INTO management_execution_scopes VALUES(?,?,?)', (ident, kind, legacy))
        physical[legacy] = ident
    witnesses = []
    if 'position_management_scopes' in tables:
        for row in conn.execute('SELECT * FROM position_management_scopes'):
            payload = json.loads(row['payload'])
            witnesses.append((row['owner'], row['account_scope'], row['symbol'], row['side'] if payload.get('hedged') is True else 'BOTH', payload))
    if 'real_protection_plans' in tables:
        for row in conn.execute('SELECT config_id,payload FROM real_protection_plans'):
            plan = json.loads(row['payload'])
            if plan.get('state') != 'DONE' and plan.get('account_scope'):
                witnesses.append((row['config_id'], plan['account_scope'], plan['symbol'], plan['side'] if plan.get('management', {}).get('hedged') is True else 'BOTH', plan))
    for owner, legacy, symbol, side, evidence in witnesses:
        alias = conn.execute('SELECT physical_account FROM management_account_aliases WHERE legacy_scope=?', (legacy,)).fetchone()
        ident = physical.get(legacy) or (alias[0] if alias else 'legacy:' + legacy)
        key = hashlib.sha256(json.dumps([ident, 'swap', symbol, side]).encode()).hexdigest()
        existing = _decode(conn.execute('SELECT * FROM visible_management_claims WHERE claim_id=?', (key,)).fetchone())
        bound = _legacy_resource_bound(conn, owner, legacy, symbol, side, evidence)
        if existing and existing['state'] == 'owned' and existing['owner'] != owner:
            raise ManagementConflict('Existing management migration contains competing owners', {'symbol': symbol, 'side': side})
        payload = {'policy': 'visible_first_claim', 'source': 'migration', 'symbol': symbol,
                                  'legacy_scope': legacy, 'legacy_policy': (configs.get(owner) or {}).get('position_management_policy') or evidence.get('policy'),
                                  'resource_bound': bound, 'exchange_profile_id': (configs.get(owner) or {}).get('exchange_profile_id'),
                                  'claimed_at': time.time(), 'observed_at': evidence.get('observed_at')}
        if not existing or existing['state'] == 'released':
            conn.execute('INSERT INTO visible_management_claims VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(claim_id) '
                         'DO UPDATE SET owner=excluded.owner,state=excluded.state,version=excluded.version,payload=excluded.payload',
                         (key, ident, 'swap', symbol, side, owner, 'owned', (existing or {}).get('version', 0) + 1, json.dumps(payload)))
        elif bound and not existing.get('resource_bound'):
            original = {k: v for k, v in existing.items() if k not in {'claim_id','physical_account','market_type','resource','side','owner','state','version'}}
            original['resource_bound'] = True
            conn.execute('UPDATE visible_management_claims SET payload=? WHERE claim_id=?', (json.dumps(original), key))


def _config(mt):
    return dict(getattr(mt, 'runtime_config', {}) or {})


def _legacy_scope(mt):
    exchange = mt.exchange
    return hashlib.sha256(f"{exchange.id}:{getattr(exchange, 'apiKey', '') or mt.config_id}".encode()).hexdigest()


def physical_account_scope(mt, *, persist=False):
    """A saved physical identity or proven credential alias, never guessed by key difference."""
    cfg = _config(mt)
    if str(cfg.get('mode', '')).upper() == 'STRATEGY':
        return 'simulation:' + str(mt.config_id)
    explicit = cfg.get('physical_account_id') or cfg.get('account_identity')
    venue = str(getattr(mt.exchange, 'id', '')).replace('binanceusdm', 'binance')
    credential = hashlib.sha256(f"{venue}:{getattr(mt.exchange, 'apiKey', '') or mt.config_id}".encode()).hexdigest()
    legacy = _legacy_scope(mt)
    identity = f'physical:{venue}:{explicit}' if explicit else 'credentials:' + credential
    with database.get_db_conn() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'management_account_aliases' in tables:
            existing = conn.execute('SELECT physical_account FROM management_account_aliases WHERE legacy_scope=?', (legacy,)).fetchone()
            if existing and not explicit:
                identity = existing[0]
            if persist:
                _bind_account_alias(conn, legacy, identity)
                kind = getattr(mt, 'market_type', cfg.get('market_type', 'swap'))
                conn.execute('INSERT OR IGNORE INTO management_execution_scopes VALUES(?,?,?)', (identity, kind, legacy))
                conn.commit()
    return identity


def _authorized(mt, symbol):
    cfg = _config(mt)
    if not getattr(mt, 'config_id', None) or cfg.get('read_only'):
        raise ValueError('A writable trusted task/profile is required for management')
    if cfg.get('enabled') is False and _active_management.get() is None and not cfg.get('_management_maintenance'):
        raise ValueError('Disabled configuration cannot claim new management authority')
    market = mt.exchange.market(symbol)
    canonical = market.get('symbol') or symbol
    expected = 'spot' if cfg.get('mode') == 'SPOT_DCA' else cfg.get('market_type', getattr(mt, 'market_type', 'swap'))
    if expected == 'spot' and not market.get('spot'):
        raise ValueError('Management target is outside the configured spot market')
    if expected != 'spot' and (market.get('linear') is False or market.get('swap') is False):
        raise ValueError('Management target is outside the configured linear perpetual market')
    allowed = cfg.get('symbols') or ([cfg['symbol']] if cfg.get('symbol') else [])
    unrestricted = cfg.get('symbol_scope', cfg.get('mcp_symbol_scope', 'selected')) == 'all'
    if not unrestricted and not any(str(s).upper().split(':')[0] == str(canonical).upper().split(':')[0] for s in allowed):
        raise ValueError('Management target symbol is outside trusted task/profile authorization')
    return canonical, expected


def _key(mt, symbol, side=None):
    canonical, market_type = _authorized(mt, symbol)
    if market_type == 'spot':
        resource = mt.exchange.market(canonical).get('base') or canonical.split('/')[0]
        return physical_account_scope(mt, persist=True), 'spot', resource.upper(), 'BASE', canonical
    mode = mt.exchange.fetch_position_mode(canonical)
    if not isinstance(mode, dict) or type(mode.get('hedged')) is not bool:
        raise ValueError('Position mode is unavailable for management coordination')
    if mode['hedged'] and side not in {'LONG', 'SHORT'}:
        raise ValueError('An exact Hedge target direction is required for management')
    return physical_account_scope(mt, persist=True), 'swap', canonical, side if mode['hedged'] else 'BOTH', canonical


def _existing_owner(conn, mt, canonical, side):
    """Import only effective owners or live evidence; terminal history is not a claim."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    legacy = _legacy_scope(mt)
    owners = set()
    if 'position_management_scopes' in tables:
        for row in conn.execute('SELECT owner,side FROM position_management_scopes WHERE account_scope=? AND symbol=?', (legacy, canonical)):
            if side == 'BOTH' or row['side'] in {side, 'BOTH'}:
                owners.add(row['owner'])
    if 'real_protection_plans' in tables:
        for row in conn.execute('SELECT config_id,payload FROM real_protection_plans WHERE symbol IN (?,?)', (canonical, canonical.split(':')[0])):
            plan = json.loads(row['payload'])
            if (plan.get('account_scope') == legacy and plan.get('state') != 'DONE'
                    and (side == 'BOTH' or plan.get('side') == side)):
                owners.add(row['config_id'])
    if len(owners) > 1:
        raise ManagementConflict('Existing management evidence contains competing owners', {'owners': sorted(owners), 'symbol': canonical, 'side': side})
    return next(iter(owners), None)


def claim_management(mt, symbol, side=None, operation_id=None):
    physical, market_type, resource, scope_side, canonical = _key(mt, symbol, side)
    identity = hashlib.sha256(json.dumps([physical, market_type, resource, scope_side]).encode()).hexdigest()
    with database.get_db_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute('SELECT * FROM visible_management_claims WHERE claim_id=?', (identity,)).fetchone()
        claim = _decode(existing)
        if _config(mt).get('enabled') is False and (not claim or claim['state'] != 'owned' or claim['owner'] != mt.config_id):
            raise ValueError('Paused maintenance cannot claim a new resource scope')
        # BOTH overlaps both Hedge directions across a position-mode change.
        competing = conn.execute("SELECT * FROM visible_management_claims WHERE physical_account=? AND market_type=? AND resource=? "
                                 "AND state='owned' AND (side=? OR side='BOTH' OR ?='BOTH') AND owner<>? LIMIT 1",
                                 (physical, market_type, resource, scope_side, scope_side, mt.config_id)).fetchone()
        if competing:
            raise ManagementConflict('Another manager owns this account resource', _decode(competing))
        prior_owner = _existing_owner(conn, mt, canonical, scope_side) if market_type == 'swap' else None
        if market_type == 'spot' and (not claim or claim['state'] != 'owned'):
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            aliases = {r[0] for r in conn.execute('SELECT legacy_scope FROM management_account_aliases WHERE physical_account=?', (physical,))}
            aliases.add(_legacy_scope(mt))
            existing_owners = {r['config_id'] for r in conn.execute("SELECT config_id,account_scope,symbol FROM mcp_spot_exits WHERE state NOT IN ('filled','cancelled','failed')")
                               if r['account_scope'] in aliases and r['symbol'].split('/')[0] == resource} if 'mcp_spot_exits' in tables else set()
            if len(existing_owners)>1:
                raise ManagementConflict('Existing live spot resources contain competing owners', {'owners':sorted(existing_owners),'base_asset':resource})
            prior_owner = next(iter(existing_owners),None)
        owner = (claim or {}).get('owner') if claim and claim['state'] == 'owned' else prior_owner or mt.config_id
        if claim is None or claim['state'] == 'released':
            payload = {'policy': 'visible_first_claim', 'symbol': canonical, 'claimed_at': time.time(),
                       'observed_at': None, 'source': 'configuration' if prior_owner else 'first_claim',
                       'operation_id': operation_id, 'legacy_scope': _legacy_scope(mt)}
            payload['exchange_profile_id'] = _config(mt).get('exchange_profile_id')
            version = (claim or {}).get('version', 0) + 1
            conn.execute('INSERT INTO visible_management_claims VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(claim_id) '
                         'DO UPDATE SET owner=excluded.owner,state=excluded.state,version=excluded.version,payload=excluded.payload',
                         (identity, physical, market_type, resource, scope_side, owner, 'owned', version, json.dumps(payload)))
            claim = {'claim_id': identity, 'physical_account': physical, 'market_type': market_type, 'resource': resource,
                     'side': scope_side, 'owner': owner, 'state': 'owned', 'version': version, **payload}
        elif owner == mt.config_id and claim.get('source') == 'migration':
            payload = {k: v for k, v in claim.items() if k not in {'claim_id','physical_account','market_type','resource','side','owner','state','version'}}
            payload['source'] = 'first_claim'
            payload['activated_at'] = time.time()
            conn.execute('UPDATE visible_management_claims SET payload=? WHERE claim_id=?', (json.dumps(payload), identity))
            claim.update(payload)
        conn.commit()
    if owner != mt.config_id:
        raise ManagementConflict('An existing manager retains this resource', claim)
    return claim


def management_status(config_id, symbol=None):
    with database.get_db_conn() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'visible_management_claims' not in tables:
            return {'policy': 'visible_first_claim', 'owner': config_id, 'scopes': [], 'resources': []}
        scopes = [_decode(row) for row in conn.execute("SELECT * FROM visible_management_claims WHERE owner=? AND state='owned'", (config_id,))]
        if symbol:
            scopes = [s for s in scopes if s.get('symbol', '').split(':')[0] == symbol.split(':')[0]
                      or s['market_type'] == 'spot' and s['resource'] == symbol.split('/')[0]]
        ids = {s['claim_id'] for s in scopes}
        resources = [_decode(row) for row in conn.execute('SELECT * FROM management_resource_links') if row['claim_id'] in ids]
    return {'policy': 'visible_first_claim', 'owner': config_id, 'scopes': scopes, 'resources': resources}


def resource_management_status(mt, symbol, side=None):
    """Pure scoped projection, including other managers; no claim or enrollment."""
    cfg = _config(mt)
    market = mt.exchange.market(symbol)
    canonical = market.get('symbol') or symbol
    market_type = 'spot' if market.get('spot') or cfg.get('mode') == 'SPOT_DCA' else 'swap'
    allowed = cfg.get('symbols') or ([cfg['symbol']] if cfg.get('symbol') else [])
    visible = cfg.get('symbol_scope', cfg.get('mcp_symbol_scope')) == 'all' or any(
        s.split(':')[0] == canonical.split(':')[0] for s in allowed)
    if cfg.get('read_only') and not allowed:
        visible = True  # Trusted temporary account reader, never a claimant.
    eligible = visible and not cfg.get('read_only') and cfg.get('enabled', True) and bool(getattr(mt, 'config_id', None))
    physical = physical_account_scope(mt)
    resource = (market.get('base') or canonical.split('/')[0]) if market_type == 'spot' else canonical
    try:
        mode = mt.exchange.fetch_position_mode(canonical) if market_type == 'swap' else {'hedged': False}
        scope_side = 'BASE' if market_type == 'spot' else side if mode.get('hedged') else 'BOTH'
    except Exception:
        return {'visible': visible, 'eligible': eligible, 'can_claim': False, 'state': 'needs_refresh',
                'owner': None, 'reason': 'Position mode evidence is unavailable'}
    with database.get_db_conn() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = list(conn.execute("SELECT * FROM visible_management_claims WHERE physical_account=? AND market_type=? AND resource=? AND state='owned'",
                                 (physical, market_type, resource))) if 'visible_management_claims' in tables else []
        legacy_rows = []
        if not rows and market_type == 'swap' and 'position_management_scopes' in tables:
            legacy_rows = list(conn.execute('SELECT * FROM position_management_scopes WHERE account_scope=? AND symbol=?', (_legacy_scope(mt), canonical)))
    claims = [_decode(r) for r in rows if scope_side is None or r['side'] in {scope_side, 'BOTH'} or scope_side == 'BOTH']
    for row in legacy_rows:
        facts = json.loads(row['payload'])
        actual_side = row['side'] if facts.get('hedged') is True else 'BOTH'
        if scope_side is None or actual_side in {scope_side, 'BOTH'} or scope_side == 'BOTH':
            claims.append({**facts, 'owner': row['owner'], 'claim_id': None, 'state': 'owned',
                           'side': actual_side, 'symbol': canonical, 'legacy_policy': True})
    competing = next((c for c in claims if c['owner'] != getattr(mt, 'config_id', None)), None)
    own = next((c for c in claims if c['owner'] == getattr(mt, 'config_id', None)), None)
    chosen = competing or own
    return {'visible': visible, 'eligible': eligible, 'can_claim': bool(eligible and not chosen),
            'state': 'conflict' if competing else 'owned' if own else 'eligible_unclaimed' if eligible else 'read_only',
            'owner': chosen['owner'] if chosen else None, 'claim_id': chosen['claim_id'] if chosen else None,
            'policy': 'visible_first_claim', 'scope': {'physical_account': physical, 'market_type': market_type,
                                                     'resource': resource, 'side': scope_side}, 'claims': claims}


def link_resource(claim, identity, original_owner=None, **metadata):
    with database.get_db_conn() as conn:
        previous = conn.execute('SELECT payload,original_owner FROM management_resource_links WHERE claim_id=? AND identity=?',
                                (claim['claim_id'], identity)).fetchone()
        payload = {**(json.loads(previous['payload']) if previous else {}), **metadata}
        conn.execute('INSERT INTO management_resource_links VALUES(?,?,?,?) ON CONFLICT(claim_id,identity) '
                     'DO UPDATE SET payload=excluded.payload',
                     (claim['claim_id'], identity, previous['original_owner'] if previous else original_owner, json.dumps(payload)))
        conn.commit()


def _claim_by_id(config_id, claim_id):
    with database.get_db_conn() as conn:
        claim = _decode(conn.execute("SELECT * FROM visible_management_claims WHERE claim_id=? AND owner=? AND state='owned'", (claim_id, config_id)).fetchone())
    if not claim:
        raise ValueError('Management claim was not found for this owner')
    return claim


def _operation_in_claim(conn, tables, claim, operation_id, symbol):
    if claim['market_type'] == 'spot':
        return symbol.split('/')[0] == claim['resource']
    if symbol.split(':')[0] != claim['symbol'].split(':')[0]:
        return False
    if claim['side'] == 'BOTH':
        return True
    sides = set()
    if 'trade_operation_requests' in tables:
        row = conn.execute('SELECT request_json FROM trade_operation_requests WHERE config_id=? AND operation_id=?',
                           (claim['owner'], operation_id)).fetchone()
        if row:
            request = json.loads(row[0])
            args = request.get('arguments') or request.get('args') or request
            sides.update(value for value in (args.get('side'), args.get('pos_side')) if value in {'LONG', 'SHORT'})
            for action in args.get('actions') or []:
                if action.get('symbol', symbol).split(':')[0] == symbol.split(':')[0] and action.get('side') in {'LONG', 'SHORT'}:
                    sides.add(action['side'])
    if 'trade_operation_evidence' in tables:
        sides.update(r[0] for r in conn.execute('SELECT DISTINCT json_extract(c.payload,\'$.side\') FROM trade_operation_evidence e '
                'JOIN trade_cycle_evidence c ON c.config_id=e.config_id AND c.account_scope=e.account_scope AND c.symbol=e.symbol AND c.episode_id=e.episode_id '
                'WHERE e.config_id=? AND e.operation_id=?',
                                                (claim['owner'], operation_id)) if r[0] in {'LONG', 'SHORT'})
    return not sides or claim['side'] in sides


def _resources(mt, claim):
    symbol = claim['symbol']
    if claim['market_type'] == 'spot':
        balance = mt.exchange.fetch_balance()
        asset = balance.get(claim['resource']) or {}
        amount = float(asset.get('total', (balance.get('total') or {}).get(claim['resource'], 0)) or 0)
        positions = [{'base_asset': claim['resource'], 'amount': amount}] if amount else []
        markets = getattr(mt.exchange, 'markets', {})
        symbols = [s for s, m in markets.items() if m.get('spot') and m.get('base') == claim['resource']] or [symbol]
        orders = [o for s in symbols for o in mt.exchange.fetch_open_orders(s)]
    else:
        positions = [p for p in mt.exchange.fetch_positions([symbol]) if float(p.get('contracts') or 0) > 0
                     and (claim['side'] == 'BOTH' or str(p.get('side', '')).upper() == claim['side'])]
        from backend.utils.position_management import _orders, _order_side
        orders = []
        hedged = claim['side'] != 'BOTH'
        for order in _orders(mt.exchange, symbol).values():
            if not hedged or _order_side(order, True)[0] == claim['side']:
                orders.append(order)
    native_rows = []
    with database.get_db_conn() as conn:
        actions = [_decode(r) for r in conn.execute("SELECT * FROM visible_management_actions WHERE claim_id=? AND state IN ('running','unknown','pending')", (claim['claim_id'],))]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'position_management_actions' in tables:
            actions.extend(dict(r) for r in conn.execute("SELECT * FROM position_management_actions WHERE owner=? AND symbol=? AND state IN ('running','unresolved','pending','unknown')",
                                                        (claim['owner'], symbol)))
        if 'trade_action_runs' in tables:
            for row in conn.execute("SELECT operation_id,symbol,status FROM trade_action_runs WHERE config_id=? AND status IN ('running','unknown','pending')", (claim['owner'],)):
                if _operation_in_claim(conn, tables, claim, row['operation_id'], row['symbol']):
                    actions.append({'operation_id': row['operation_id'], 'symbol': row['symbol'],
                                    'state': row['status'], 'source_owner': claim['owner']})
        if claim['owner'].startswith('mcp:') and {'mcp_operations', 'mcp_operation_links'} <= tables:
            for row in conn.execute("SELECT l.internal_operation_id,l.symbol,o.state FROM mcp_operations o JOIN mcp_operation_links l "
                                    "ON l.profile_id=o.profile_id AND l.operation_id=o.operation_id WHERE o.profile_id=? "
                                    "AND o.state IN ('started','unknown','pending')", (claim['owner'][4:],)):
                if _operation_in_claim(conn, tables, claim, row['internal_operation_id'], row['symbol']):
                    actions.append({'operation_id': row['internal_operation_id'], 'symbol': row['symbol'],
                                    'state': 'running' if row['state'] == 'started' else row['state'], 'source_owner': claim['owner']})
        if claim['market_type'] == 'spot' and 'mcp_spot_exits' in tables:
            aliases = {r[0] for r in conn.execute('SELECT legacy_scope FROM management_account_aliases WHERE physical_account=?',
                                                 (claim['physical_account'],))}
            aliases.add(claim.get('legacy_scope'))
            for row in conn.execute("SELECT * FROM mcp_spot_exits WHERE state NOT IN ('filled','cancelled','failed')"):
                if row['account_scope'] in aliases and row['symbol'].split('/')[0] == claim['resource']:
                    native_rows.append({**dict(row),'payload':json.loads(row['payload']),'_visible_management':True})
    for row in native_rows:
        from backend.mcp.spot_orders import _refresh, _EXIT_TERMINAL
        was_unknown = row['state'] in {'submitting','cancelling','unknown','pending'}
        try:
            _refresh(mt.exchange,row)
            if row['state'] not in _EXIT_TERMINAL:
                orders.append({'id':row['exit_id'],'symbol':row['symbol'],'native':True,'state':row['state'],'amount':row['amount']})
        except Exception:
            actions.append({'operation_id':row['operation_id'],'state':'unknown','recovery_kind':'spot_exit',
                            'exit_id':row['exit_id'],'source_owner':row['config_id'],'symbol':row['symbol']})
        # Preserve submission identity even after the order is now visible.
        if was_unknown:
            actions.append({'operation_id':row['operation_id'],'state':'unknown','recovery_kind':'spot_exit',
                            'exit_id':row['exit_id'],'source_owner':row['config_id'],'symbol':row['symbol']})
    return {'positions': positions, 'orders': orders, 'unresolved': actions, 'observed_at': time.time()}


def release_management_scope(config_id, claim_id, *, confirmed=False):
    if not confirmed:
        raise ValueError('Explicit confirmation is required to release management')
    from backend.utils.market_data import MarketTool
    mt = MarketTool(config_id=config_id)
    claim = _claim_by_id(config_id, claim_id)
    with scope_file_lock(claim['physical_account'], claim['resource'], claim['side']):
        resources = _resources(mt, claim)
        if any(resources[k] for k in ('positions', 'orders', 'unresolved')):
            return {'status': 'blocked', 'claim_id': claim_id, 'conflicts': ['active_or_unresolved_resources'], 'resources': resources}
        with database.get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            changed = conn.execute("UPDATE visible_management_claims SET state='released',version=version+1 WHERE claim_id=? AND owner=? AND version=?",
                                   (claim_id, config_id, claim['version']))
            if changed.rowcount != 1:
                raise ValueError('Management scope changed during retirement')
            conn.execute('INSERT INTO management_scope_retirements VALUES(?,?,?,?,?,?,?,?)',
                         (claim_id, config_id, claim['version'], claim['market_type'], claim['resource'], claim['symbol'],
                          'released', json.dumps({'observed_at': resources['observed_at'], 'explicit': True})))
            if claim['market_type'] == 'swap':
                conn.execute("DELETE FROM position_management_scopes WHERE owner=? AND symbol=? AND (side=? OR ?='BOTH')",
                             (config_id, claim['symbol'], claim['side'], claim['side']))
            conn.commit()
    return {'status': 'released', 'claim_id': claim_id, 'resources': resources, 'conflicts': []}


def transfer_management_scope(config_id, claim_id, target_config_id, *, accepted=False):
    if not accepted or target_config_id == config_id:
        raise ValueError('The receiving management task must explicitly accept this transfer')
    from backend.utils.market_data import MarketTool
    source = MarketTool(config_id=config_id)
    target = MarketTool(config_id=target_config_id)
    claim = _claim_by_id(config_id, claim_id)
    _authorized(target, claim['symbol'])
    if physical_account_scope(target) != claim['physical_account']:
        raise ValueError('Management transfer requires the same trusted physical account')
    with scope_file_lock(claim['physical_account'], claim['resource'], claim['side']):
        resources = _resources(source, claim)
        if any(a.get('state') == 'running' for a in resources['unresolved']):
            return {'status': 'blocked', 'claim_id': claim_id, 'resources': resources, 'conflicts': ['original_worker_running']}
        with database.get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            updated = conn.execute("UPDATE visible_management_claims SET owner=?,version=version+1 WHERE claim_id=? AND owner=? AND version=?",
                                   (target_config_id, claim_id, config_id, claim['version']))
            if updated.rowcount != 1:
                raise ValueError('Management changed during handoff')
            conn.execute('INSERT INTO management_scope_retirements VALUES(?,?,?,?,?,?,?,?)',
                         (claim_id, config_id, claim['version'], claim['market_type'], claim['resource'], claim['symbol'],
                          'transferred', json.dumps({'receiver': target_config_id, 'accepted': True, 'observed_at': resources['observed_at']})))
            for item in resources['unresolved']:
                operation = item['operation_id']
                recovery_id = 'handoff:' + hashlib.sha256(f'{claim_id}:{operation}:{target_config_id}'.encode()).hexdigest()[:32]
                conn.execute('INSERT INTO management_recovery_grants VALUES(?,?,?,?,?,?) ON CONFLICT(recovery_id) '
                             'DO UPDATE SET payload=CASE WHEN json_extract(excluded.payload,\'$.recovery_kind\') IS NOT NULL THEN excluded.payload ELSE management_recovery_grants.payload END',
                             (recovery_id, claim_id, target_config_id, item.get('source_owner', item.get('owner', config_id)), operation,
                              json.dumps({'symbol': item.get('symbol', claim['symbol']), 'account_scope': claim.get('legacy_scope'),
                                          'side': claim['side'], 'market_type': claim['market_type'],
                                          'query_only': True, 'recovery_kind': item.get('recovery_kind'), 'exit_id': item.get('exit_id')})))
            if claim['market_type'] == 'spot' and 'mcp_spot_exits' in tables:
                aliases = {r[0] for r in conn.execute('SELECT legacy_scope FROM management_account_aliases WHERE physical_account=?',
                                                     (claim['physical_account'],))}
                aliases.add(claim.get('legacy_scope'))
                for row in conn.execute("SELECT * FROM mcp_spot_exits WHERE state NOT IN ('filled','cancelled','failed')"):
                    if row['account_scope'] in aliases and row['symbol'].split('/')[0] == claim['resource']:
                        conn.execute('INSERT INTO management_resource_links VALUES(?,?,?,?) ON CONFLICT(claim_id,identity) '
                                     'DO UPDATE SET payload=excluded.payload',
                                     (claim_id, row['exit_id'], row['config_id'], json.dumps({'kind': 'spot_exit', 'symbol': row['symbol'],
                                      'operation_id': row['operation_id'], 'state': row['state'], 'source': 'agent', 'accepted_by': target_config_id})))
            conn.execute('UPDATE position_management_scopes SET owner=? WHERE account_scope=? AND symbol=? '
                         "AND (side=? OR ?='BOTH')",
                         (target_config_id, claim.get('legacy_scope'), claim['symbol'], claim['side'], claim['side']))
            # A plan's execution actor and ledger links remain unchanged. Store a
            # receiver copy with original actor, while retiring old mutable view.
            rows = list(conn.execute('SELECT side,payload FROM real_protection_plans WHERE config_id=? AND symbol=?', (config_id, claim['symbol'])))
            for row in rows:
                if claim['side'] != 'BOTH' and row['side'] != claim['side']:
                    continue
                plan = json.loads(row['payload'])
                plan['original_owner'] = plan.get('original_owner') or config_id
                plan['management_owner'] = target_config_id
                plan.setdefault('management', {}).update(owner=target_config_id, claim_id=claim_id,
                                                         claim_version=claim['version'] + 1)
                conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(?,?,?,?) '
                             'ON CONFLICT(config_id,symbol,side) DO UPDATE SET payload=excluded.payload',
                             (target_config_id, claim['symbol'], row['side'], json.dumps(plan)))
                plan.update(state='DONE', transferred_to=target_config_id)
                conn.execute('UPDATE real_protection_plans SET payload=? WHERE config_id=? AND symbol=? AND side=?',
                             (json.dumps(plan), config_id, claim['symbol'], row['side']))
            conn.commit()
    return {'status': 'transferred', 'claim_id': claim_id, 'owner': target_config_id, 'resources': resources,
            'conflicts': [], 'recovery_targets': management_recovery_targets(target_config_id)}


def management_recovery_targets(config_id):
    with database.get_db_conn() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'management_recovery_grants' not in tables:
            return []
        return [_decode(r) for r in conn.execute("SELECT g.* FROM management_recovery_grants g JOIN visible_management_claims c ON c.claim_id=g.claim_id "
                                                "WHERE g.receiver=? AND c.owner=? AND c.state='owned'", (config_id, config_id))]


def lookup_management_recovery(config_id, recovery_id):
    return next((r for r in management_recovery_targets(config_id) if r['recovery_id'] == recovery_id), None)


def reconcile_visible_actions(config_id):
    from backend.utils.trade_operations import refresh_operation_receipt, tool_result_status
    from backend.utils.management_lock import ManagementLockBusy
    from backend.utils.position_protection import _lock
    with database.get_db_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT a.*,c.physical_account,c.resource,c.side,c.payload AS claim_payload,"
                                            "s.legacy_scope AS execution_scope FROM visible_management_actions a "
                                            "JOIN visible_management_claims c ON c.claim_id=a.claim_id "
                                            "LEFT JOIN management_execution_scopes s ON s.physical_account=c.physical_account "
                                            "AND s.market_type=c.market_type WHERE c.owner=? "
                                            "AND a.state IN ('running','pending','unknown')", (config_id,))]
    changed = 0
    for row in rows:
        try:
            scope = row['execution_scope'] or json.loads(row['claim_payload'])['legacy_scope']
            # The original-order recovery below takes the same account lock.
            # Acquire it before the process lock, as financial actions do.
            with _lock(scope), scope_file_lock(row['physical_account'], row['resource'], row['side']):
                metadata = json.loads(row['payload'])
                if row['owner'] != config_id:
                    grant = next((r for r in management_recovery_targets(config_id) if r['operation_id'] == row['operation_id']), None)
                    receipt = query_management_recovery(config_id, grant['recovery_id']).get('result') if grant else None
                else:
                    from backend.agent.unified_trading import query_unified_operation
                    receipt = query_unified_operation(row['owner'], metadata['symbol'], row['operation_id'], read_exchange=True)
                    if receipt is None:
                        receipt = refresh_operation_receipt(row['owner'], metadata['symbol'], row['operation_id'], read_exchange=True)
                if (receipt and tool_result_status(receipt) in {'submitted', 'completed', 'failed', 'skipped'}
                        and (receipt.get('query') or {}).get('status') in {'completed', 'partial'}
                        and receipt.get('error_code') not in {'operation_not_found', 'account_scope_mismatch', 'accepted_scope_evidence_unavailable'}):
                    with database.get_db_conn() as conn:
                        conn.execute("UPDATE visible_management_actions SET state='completed' WHERE claim_id=? AND operation_id=?",
                                     (row['claim_id'], row['operation_id']))
                        conn.commit()
                        changed += 1
        except ManagementLockBusy:
            continue
        except Exception:
            continue  # Unknown original identity remains reserved; never resubmit.
    return changed


def query_management_recovery(config_id, recovery_id):
    grant = lookup_management_recovery(config_id, recovery_id)
    if not grant:
        return {'status': 'failed', 'state': 'not_found', 'operation_id': recovery_id}
    from backend.utils.trade_operations import refresh_operation_receipt
    if grant.get('recovery_kind') == 'spot_exit':
        from backend.utils.market_data import MarketTool
        from backend.mcp.spot_orders import _managed_rows, _refresh, _receipt
        mt = MarketTool(config_id=config_id)
        row = next((r for r in _managed_rows(mt, grant['symbol']) if r['exit_id'] == grant['exit_id']), None)
        if row:
            try:
                _refresh(mt.exchange, row)
                result = _receipt(row)
                result['query'] = {'status': 'completed', 'queried_at': time.time(), 'source': 'exchange_order'}
            except Exception as exc:
                result = {'status': 'unknown', 'query': {'status': 'failed'}, 'error': type(exc).__name__}
        else:
            result = None
    else:
        from backend.utils.market_data import MarketTool
        from backend.utils.independent_exits import IndependentExits
        from backend.utils.trade_recovery import recover_operation
        try:
            mt = MarketTool(config_id=config_id)
            service = IndependentExits(mt)
            if grant.get('account_scope') and service.account_scope != grant['account_scope']:
                with database.get_db_conn() as conn:
                    alias = conn.execute('SELECT physical_account FROM management_account_aliases WHERE legacy_scope=?',
                                         (grant['account_scope'],)).fetchone()
                if not alias or alias[0] != physical_account_scope(mt):
                    raise ValueError('Accepted recovery account identity changed')
            # Use the receiver's authorized credentials, while reading and
            # updating only the granted original execution actor's evidence.
            service.config_id = grant['source_owner']
            from backend.agent.unified_trading import query_unified_operation
            result = query_unified_operation(grant['source_owner'], grant['symbol'], grant['operation_id'],
                                             read_exchange=True, market_tool=mt, recovery_scope=grant)
            if result is None:
                result = recover_operation(grant['source_owner'], grant['symbol'], grant['operation_id'], service=service, read_exchange=True)
        except Exception as exc:
            result = {'status': 'unknown', 'query': {'status': 'failed'}, 'error': type(exc).__name__}
    result = result or {'status': 'failed', 'state': 'not_found'}
    if (grant.get('market_type') == 'swap' and grant.get('side') in {'LONG', 'SHORT'}
            and result.get('contract_version') != 3):
        def accepted(item):
            value = item.get('result') if isinstance(item.get('result'), dict) else item
            scoped = value.get('current_scope') or value.get('scope') or {}
            observed_side = value.get('side') or value.get('pos_side') or scoped.get('side')
            return observed_side == grant['side']
        if isinstance(result.get('results'), list):
            selected = [item for item in result['results'] if isinstance(item, dict) and accepted(item)]
            result = {'status': result.get('status'), 'results': selected, 'query': result.get('query'),
                      'scope': {'symbol': grant['symbol'], 'side': grant['side']}}
        elif not accepted(result):
            result = {'status': 'unknown', 'query': result.get('query', {'status': 'failed'}),
                      'error': 'Accepted resource execution evidence is not yet available'}
    from backend.utils.trade_operations import tool_result_status
    if (tool_result_status(result) in {'completed', 'failed', 'skipped', 'submitted'}
            and (result.get('query') or {}).get('status') in {'completed', 'partial'}
            and result.get('error_code') not in {'operation_not_found', 'account_scope_mismatch', 'accepted_scope_evidence_unavailable'}):
        with database.get_db_conn() as conn:
            conn.execute("UPDATE visible_management_actions SET state='completed' WHERE claim_id=? AND operation_id=? AND state<>'running'",
                         (grant['claim_id'], grant['operation_id']))
            conn.commit()
    return {'operation_id': recovery_id, 'status': result.get('status'), 'query_only': True, 'result': result}


def assert_configuration_claims(conn, owner, updated=None):
    """Deleting/changing boundaries requires explicit release, even when flat."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'visible_management_claims' not in tables:
        return
    claims = [_decode(r) for r in conn.execute("SELECT * FROM visible_management_claims WHERE owner=? AND state='owned'", (owner,))]
    if not claims:
        return
    allowed = (updated or {}).get('symbols') or ([(updated or {})['symbol']] if (updated or {}).get('symbol') else [])
    unrestricted = (updated or {}).get('symbol_scope', (updated or {}).get('mcp_symbol_scope')) == 'all'
    for claim in claims:
        if (claim.get('source') == 'migration' and claim.get('legacy_policy') == 'exclusive_scope'
                and claim.get('resource_bound') is False and (updated or {}).get('position_management_policy') != 'visible_first_claim'):
            from backend.utils.position_management import _scope_active
            witnesses = list(conn.execute('SELECT * FROM position_management_scopes WHERE owner=? AND account_scope=? AND symbol=?',
                                           (owner, claim.get('legacy_scope'), claim['symbol'])))
            if witnesses and not any(_scope_active(conn, row) for row in witnesses):
                continue  # Only a legacy reservation that never bound a real resource may retire implicitly.
        if updated is not None and claim.get('exchange_profile_id') and updated.get('exchange_profile_id') != claim['exchange_profile_id']:
            if updated.get('_management_account_identity') != claim['physical_account']:
                raise ValueError('Owned active management account must be released or explicitly transferred before changing account credentials')
        if updated is not None and updated.get('_management_account_identity') and updated['_management_account_identity'] != claim['physical_account']:
            raise ValueError('Owned active management account must be released or explicitly transferred before changing account credentials')
        expected_kind = 'spot' if (updated or {}).get('mode') == 'SPOT_DCA' else (updated or {}).get('market_type', 'swap')
        if updated is not None and ((updated.get('mode') == 'STRATEGY') or expected_kind != claim['market_type']):
            raise ValueError('Owned management resources must be released or transferred before changing trading modes')
        if not unrestricted and not any(s.split(':')[0] == claim['symbol'].split(':')[0] for s in allowed):
            raise ValueError('Owned management authority has active or unresolved responsibility; explicitly release or transfer before shrinking symbols')


def retired_management_resources(config_id, market_type='spot'):
    """Explicit local retirements, never inferred from old trades or elapsed time."""
    with database.get_db_conn() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'management_scope_retirements' not in tables:
            return set()
        retired = {r[0] for r in conn.execute('SELECT resource FROM management_scope_retirements WHERE owner=? AND market_type=?',
                                            (config_id, market_type))}
        active = {r[0] for r in conn.execute("SELECT resource FROM visible_management_claims WHERE owner=? AND market_type=? AND state='owned'",
                                           (config_id, market_type))}
    return retired - active


def assert_current_owner(conn, owner, legacy_scope, symbol, side, *, claim_id=None, version=None):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'visible_management_claims' not in tables:
        return
    if claim_id:
        rows = list(conn.execute('SELECT * FROM visible_management_claims WHERE claim_id=?', (claim_id,)))
    else:
        alias = conn.execute('SELECT physical_account FROM management_account_aliases WHERE legacy_scope=?', (legacy_scope,)).fetchone()
        rows = list(conn.execute("SELECT * FROM visible_management_claims WHERE physical_account=? AND market_type='swap' AND resource=? AND state='owned' AND side IN (?,'BOTH')",
                                 (alias[0], symbol, side))) if alias else []
    if any(r['owner'] != owner or r['state'] != 'owned' or version is not None and r['version'] != version for r in rows):
        raise ManagementConflict('Management version changed; this worker may only preserve original execution evidence')


@contextmanager
def financial_reservation(mt, claim, operation_id):
    active = _active_management.get()
    if active:
        if active['claim_id'] != claim['claim_id']:
            raise ValueError('A nested financial action crosses its management reservation')
        yield active
        return
    with scope_file_lock(claim['physical_account'], claim['resource'], claim['side']):
        with database.get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = _decode(conn.execute('SELECT * FROM visible_management_claims WHERE claim_id=?', (claim['claim_id'],)).fetchone())
            if not current or current['owner'] != mt.config_id or current['version'] != claim['version'] or current['state'] != 'owned':
                raise ManagementConflict('Management changed before submission', current)
            old = conn.execute("SELECT * FROM visible_management_actions WHERE claim_id=? AND state IN ('running','unknown','pending')", (claim['claim_id'],)).fetchone()
            if old and old['operation_id'] != operation_id:
                raise ValueError('An original financial operation in this scope requires verification')
            conn.execute('INSERT INTO visible_management_actions VALUES(?,?,?,?,?,?) ON CONFLICT(claim_id) '
                         'DO UPDATE SET owner=excluded.owner,version=excluded.version,operation_id=excluded.operation_id,state=excluded.state,payload=excluded.payload',
                         (claim['claim_id'], mt.config_id, claim['version'], operation_id, 'running', json.dumps({'symbol': claim['symbol']})))
            conn.commit()
        token = _active_management.set(claim)
        state = 'unknown'
        try:
            yield claim
            state = claim.get('outcome_status', 'completed')
            if state == 'submitted':
                state = 'completed'  # An accepted order does not keep its submission lock.
        except Exception:
            from backend.utils.trade_receipts import current_attempt
            attempt = current_attempt.get()
            if claim.get('outcome_status') == 'failed' or attempt is not None and attempt.submission_state == 'not_submitted':
                state = 'failed'
            raise
        finally:
            _active_management.reset(token)
            with database.get_db_conn() as conn:
                conn.execute('UPDATE visible_management_actions SET state=? WHERE claim_id=? AND operation_id=?', (state, claim['claim_id'], operation_id))
                conn.commit()
