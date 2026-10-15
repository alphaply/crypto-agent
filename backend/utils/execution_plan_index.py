"""Durable, bounded ownership migration for old execution plans.

Cross-page positive regular-order evidence is collected before any ambiguous
algo alias is retired. Publishing links never sends exchange requests.
"""
from __future__ import annotations

import hashlib
import json
import time

from backend.database import get_db_conn
from backend.utils.runtime_capacity import positive_setting, reserve_bytes, capacity_limits_enabled


def initialize_schema(cursor):
    cursor.execute('''CREATE TABLE IF NOT EXISTS execution_plan_index_runs (
        index_id TEXT PRIMARY KEY, account_scope TEXT NOT NULL, symbol TEXT NOT NULL, config_id TEXT NOT NULL,
        phase TEXT NOT NULL, cursor INTEGER NOT NULL,
        event_cursor INTEGER NOT NULL DEFAULT 0, legacy_cursor INTEGER NOT NULL DEFAULT 0,
        error TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS execution_plan_index_facts (
        id INTEGER PRIMARY KEY, index_id TEXT NOT NULL, owner TEXT NOT NULL,
        order_id TEXT NOT NULL, regular INTEGER NOT NULL DEFAULT 0,
        conditional INTEGER NOT NULL DEFAULT 0, checked INTEGER NOT NULL DEFAULT 0,
        UNIQUE(index_id,owner,order_id))''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_plan_facts_page ON execution_plan_index_facts(index_id,id)')
    cursor.execute('''CREATE TABLE IF NOT EXISTS execution_plan_index_records (
        id INTEGER PRIMARY KEY, index_id TEXT NOT NULL, source TEXT NOT NULL,
        source_id INTEGER NOT NULL, owner TEXT NOT NULL, fingerprint TEXT NOT NULL,
        payload TEXT NOT NULL, UNIQUE(index_id,source,source_id))''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_plan_records_page ON execution_plan_index_records(index_id,id)')
    cursor.execute('''CREATE TABLE IF NOT EXISTS execution_plan_index_affected (
        index_id TEXT NOT NULL, owner TEXT NOT NULL, PRIMARY KEY(index_id,owner))''')


class IdentitySet:
    """Set membership backed by SQLite rather than an unbounded Python set."""
    def __init__(self, index_id, kind, values=None):
        self.index_id, self.kind, self.values = index_id, kind, values
        self.connection = None

    def add(self, identity):
        if identity is None:
            return
        owner, order_id = identity
        if self.connection is not None:
            self.connection.execute(f'''INSERT INTO execution_plan_index_facts(index_id,owner,order_id,{self.kind})
                VALUES(?,?,?,1) ON CONFLICT(index_id,owner,order_id)
                DO UPDATE SET {self.kind}=1,checked=0''', (self.index_id, owner, order_id))
            return
        with get_db_conn() as conn:
            conn.execute(f'''INSERT INTO execution_plan_index_facts(index_id,owner,order_id,{self.kind})
                VALUES(?,?,?,1) ON CONFLICT(index_id,owner,order_id)
                DO UPDATE SET {self.kind}=1,checked=0''', (self.index_id, owner, order_id))
            conn.commit()

    def __contains__(self, identity):
        if identity is None:
            return False
        if self.connection is not None:
            return self.connection.execute(f'SELECT 1 FROM execution_plan_index_facts '
                                           f'WHERE index_id=? AND owner=? AND order_id=? AND {self.kind}=1',
                                           (self.index_id, *identity)).fetchone() is not None
        with get_db_conn() as conn:
            return conn.execute(f'SELECT 1 FROM execution_plan_index_facts '
                                f'WHERE index_id=? AND owner=? AND order_id=? AND {self.kind}=1',
                                (self.index_id, *identity)).fetchone() is not None

    def __iter__(self):
        if self.values is None:
            raise ValueError('Unbounded identity iteration is forbidden')
        return iter(self.values)


def _plan_facts(index_id, owner, plan):
    from backend.utils.exit_orders import is_conditional, parse_order_reference
    with get_db_conn() as conn:
        def add(kind, value):
            if value is not None:
                raw = parse_order_reference(value)[1]
                conn.execute(f'''INSERT INTO execution_plan_index_facts(index_id,owner,order_id,{kind})
                    VALUES(?,?,?,1) ON CONFLICT(index_id,owner,order_id)
                    DO UPDATE SET {kind}=1,checked=0''', (index_id, owner, raw))
        for entry in plan.get('entries', []):
            if entry.get('id'):
                add('regular', entry['id'])
        if (plan.get('exit_order') or {}).get('id'):
            add('regular', plan['exit_order']['id'])
        for record in [*plan.get('legs', []), *plan.get('exits', [])]:
            if record.get('id'):
                kind = 'conditional' if plan.get('execution_mode') == 'independent_exits' and is_conditional(record) else 'regular'
                add(kind, record['id'])
            if record.get('execution_order_id'):
                add('regular', record['execution_order_id'])
        conn.commit()


def _source(ledger, source, cursor, limit):
    with get_db_conn() as conn:
        if source == 'legacy':
            rows = conn.execute("SELECT id AS source_id,reason IS NULL AS no_reason "
                                "FROM orders WHERE config_id=? AND trade_mode='REAL' AND (symbol=? OR symbol=?) "
                                'AND id>? ORDER BY id LIMIT ?',
                                (ledger.config_id, ledger.symbol, ledger.symbol.split(':')[0], cursor, limit)).fetchall()
            table, column = 'orders', 'reason'
        else:
            table = 'real_protection_events' if source == 'events' else 'real_protection_plans'
            column = 'payload'
            rows = conn.execute(f'SELECT rowid AS source_id FROM {table} '
                                'WHERE symbol=? AND rowid>? ORDER BY rowid LIMIT ?',
                                (ledger.symbol, cursor, limit)).fetchall()
        results = []
        for row in rows:
            if source == 'legacy' and row['no_reason']:
                size = 0
            else:
                with conn.blobopen(table, column, row['source_id'], readonly=True) as blob:
                    size = len(blob)
            results.append({'source_id': row['source_id'], 'payload_bytes': size + (1024 if source == 'legacy' else 0)})
        return results


def _load(source, source_id):
    table = {'events': 'real_protection_events', 'plans': 'real_protection_plans', 'legacy': 'orders'}[source]
    from backend.utils.sqlite_blob import bounded_text
    maximum = positive_setting('RUNTIME_MAX_RECORD_BYTES', 4 * 1024 * 1024)
    with get_db_conn() as conn:
        fields = ('order_id,config_id,side,entry_price,take_profit,stop_loss,event_type,reason IS NULL AS no_reason'
                  if source == 'legacy' else 'config_id')
        row = conn.execute(f'SELECT {fields} FROM {table} WHERE rowid=?', (source_id,)).fetchone()
        if row is None:
            raise ValueError('Ownership source changed during scan')
        if source == 'legacy':
            value = dict(row)
            if row['no_reason']:
                value['reason'] = None
            else:
                with bounded_text(conn, table, 'reason', source_id, limit=maximum) as reason:
                    value['reason'] = reason
            raw = json.dumps(value, sort_keys=True, default=str)
        else:
            with bounded_text(conn, table, 'payload', source_id, limit=maximum) as raw:
                value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError('Ownership source is not an object')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        return row['config_id'], value, digest, raw


def advance_index(ledger):
    from backend.utils.runtime_capacity import CapacityBusy
    try:
        # One source record and its parsed/derived views are resident at once.
        with reserve_bytes('parse', positive_setting('RUNTIME_MAX_RECORD_BYTES', 4 * 1024 * 1024) * 4):
            return _advance_index(ledger)
    except CapacityBusy:
        return {'complete': False, 'phase': 'capacity', 'error': 'Ownership index resource budget unavailable'}


def _advance_index(ledger):
    index_id = hashlib.sha256(json.dumps([ledger.scope, ledger.symbol, ledger.config_id]).encode()).hexdigest()
    batch = positive_setting('RUNTIME_SCAN_BATCH_SIZE', 32)
    maximum = positive_setting('RUNTIME_MAX_RECORD_BYTES', 4 * 1024 * 1024)
    limits_enabled = capacity_limits_enabled()
    deadline = time.monotonic() + positive_setting('RUNTIME_INDEX_SCAN_SECONDS', .25, minimum=.001)
    with get_db_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO execution_plan_index_runs(index_id,account_scope,symbol,config_id,phase,cursor) "
                     "VALUES(?,?,?,?,'events',0)", (index_id, ledger.scope, ledger.symbol, ledger.config_id))
        run = dict(conn.execute('SELECT * FROM execution_plan_index_runs WHERE index_id=?', (index_id,)).fetchone())
        if run['phase'] == 'complete':
            run.update(phase='events', cursor=run['event_cursor'], error=None)
        conn.commit()
    processed = 0
    try:
        while processed < batch and (processed == 0 or time.monotonic() < deadline):
            phase, cursor = run['phase'], run['cursor']
            if phase in {'events', 'plans', 'legacy'}:
                references = _source(ledger, phase, cursor, batch - processed)
                for reference in references:
                    if limits_enabled and reference['payload_bytes'] > maximum:
                        raise ValueError('Ownership source exceeds resource budget; original evidence retained')
                    owner, value, fingerprint, raw = _load(phase, reference['source_id'])
                    relevant = phase == 'legacy' or (value.get('account_scope') == ledger.scope or
                                not value.get('account_scope') and owner == ledger.config_id)
                    if relevant:
                        if phase == 'legacy':
                            if str(value.get('side') or '').upper() in {'BUY', 'SELL'} and str(value.get('event_type') or '').upper() == 'ORDER_CREATED':
                                from backend.utils.exit_orders import parse_order_reference
                                IdentitySet(index_id, 'regular').add((owner, parse_order_reference(value['order_id'])[1]))
                        else:
                            _plan_facts(index_id, owner, value)
                        with get_db_conn() as conn:
                            conn.execute('INSERT OR IGNORE INTO execution_plan_index_records(index_id,source,source_id,owner,fingerprint,payload) '
                                         'VALUES(?,?,?,?,?,?)', (index_id, phase, reference['source_id'], owner, fingerprint, raw))
                            conn.commit()
                    del value, raw
                    cursor = reference['source_id']
                    run['cursor'] = cursor
                    processed += 1
                if len(references) < batch - (processed - len(references)):
                    if phase == 'events':
                        run.update(event_cursor=cursor, phase='plans', cursor=0)
                    elif phase == 'plans':
                        run.update(phase='legacy', cursor=run['legacy_cursor'])
                    else:
                        run.update(legacy_cursor=cursor, phase='aliases', cursor=0)
            elif phase == 'aliases':
                with get_db_conn() as conn:
                    facts = conn.execute('SELECT id,owner,order_id FROM execution_plan_index_facts '
                                         'WHERE index_id=? AND id>? AND conditional=1 AND checked=0 ORDER BY id LIMIT ?',
                                         (index_id, cursor, batch - processed)).fetchall()
                for fact in facts:
                    conditional = IdentitySet(index_id, 'conditional', [(fact['owner'], fact['order_id'])])
                    _, _, affected = ledger._retire_legacy_algo_aliases((), (), conditional_ids=conditional,
                                                                       regular_ids=IdentitySet(index_id, 'regular'))
                    with get_db_conn() as conn:
                        conn.execute('UPDATE execution_plan_index_facts SET checked=1 WHERE id=?', (fact['id'],))
                        for owner in affected:
                            conn.execute('INSERT OR IGNORE INTO execution_plan_index_affected VALUES(?,?)', (index_id, owner))
                        conn.commit()
                    run['cursor'] = fact['id']
                    processed += 1
                if len(facts) < batch - (processed - len(facts)):
                    run.update(phase='publish', cursor=0)
            elif phase == 'publish':
                with get_db_conn() as conn:
                    records = conn.execute('SELECT id,source,owner '
                                           'FROM execution_plan_index_records WHERE index_id=? AND id>? ORDER BY id LIMIT ?',
                                           (index_id, cursor, batch - processed)).fetchall()
                for record in records:
                    with get_db_conn() as conn:
                        from backend.utils.sqlite_blob import bounded_text
                        with bounded_text(conn, 'execution_plan_index_records', 'payload', record['id'], limit=maximum) as raw:
                            value = json.loads(raw)
                    owner = record['owner']
                    if record['source'] == 'legacy':
                        indexed = ledger._index_legacy_row(value, IdentitySet(index_id, 'conditional'), IdentitySet(index_id, 'regular'))
                    else:
                        indexed = ledger._index_plan(owner, value)
                    if indexed:
                        with get_db_conn() as conn:
                            conn.execute('INSERT OR IGNORE INTO execution_plan_index_affected VALUES(?,?)', (index_id, owner))
                            conn.commit()
                    del value, raw
                    run['cursor'] = record['id']
                    processed += 1
                if len(records) < batch - (processed - len(records)):
                    run.update(phase='history', cursor=0)
            else:
                from backend.utils.execution_ledger import rebuild_execution_position_history
                with get_db_conn() as conn:
                    owners = conn.execute('SELECT owner FROM execution_plan_index_affected WHERE index_id=? LIMIT ?',
                                          (index_id, batch - processed)).fetchall()
                for owner in owners:
                    result = rebuild_execution_position_history(owner[0], ledger.scope, ledger.symbol)
                    if result.get('complete') is False:
                        return {'complete': False, 'phase': 'history', 'processed': processed, 'error': result.get('error')}
                    with get_db_conn() as conn:
                        conn.execute('DELETE FROM execution_plan_index_affected WHERE index_id=? AND owner=?', (index_id, owner[0]))
                        conn.commit()
                    processed += 1
                with get_db_conn() as conn:
                    has_more = conn.execute('SELECT 1 FROM execution_plan_index_affected WHERE index_id=? LIMIT 1',
                                            (index_id,)).fetchone()
                if not has_more:
                    run.update(phase='complete', cursor=0, error=None)
                    with get_db_conn() as conn:
                        conn.execute('DELETE FROM execution_plan_index_records WHERE index_id=?', (index_id,))
                        conn.commit()
                    break
            with get_db_conn() as conn:
                conn.execute('UPDATE execution_plan_index_runs SET phase=?,cursor=?,event_cursor=?,legacy_cursor=?,error=NULL WHERE index_id=?',
                             (run['phase'], run['cursor'], run['event_cursor'], run['legacy_cursor'], index_id))
                conn.commit()
    except (ValueError, TypeError) as exc:
        # Only internal stable validation text is exposed; raw JSON/credentials
        # and upstream exception representations are never included.
        run['error'] = type(exc).__name__
        with get_db_conn() as conn:
            conn.execute('UPDATE execution_plan_index_runs SET phase=?,cursor=?,event_cursor=?,legacy_cursor=?,error=? WHERE index_id=?',
                         (run['phase'], run['cursor'], run['event_cursor'], run['legacy_cursor'], run['error'], index_id))
            conn.commit()
        return {'complete': False, 'phase': run['phase'], 'processed': processed,
                'error': 'Ownership index cannot verify complete evidence within resource budget'}
    with get_db_conn() as conn:
        conn.execute('UPDATE execution_plan_index_runs SET phase=?,cursor=?,event_cursor=?,legacy_cursor=?,error=? WHERE index_id=?',
                     (run['phase'], run['cursor'], run['event_cursor'], run['legacy_cursor'], run['error'], index_id))
        conn.commit()
    return {'complete': run['phase'] == 'complete', 'phase': run['phase'], 'processed': processed,
            'error': None if run['phase'] == 'complete' else 'Ownership index scan continues in the next bounded page'}
