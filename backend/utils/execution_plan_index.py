"""Complete, paginated ownership migration for old execution plans.

Cross-page positive regular-order evidence is collected before any ambiguous
algo alias is retired. Publishing links never sends exchange requests.
"""
from __future__ import annotations

import hashlib
import json
from time import monotonic

from backend.database import get_db_conn
from backend.utils.runtime_capacity import positive_setting


def initialize_schema(cursor):
    """Schema is installed by the explicit PostgreSQL migration command."""
    return None


class IdentitySet:
    """Set membership backed by PostgreSQL rather than an unbounded Python set."""
    def __init__(self, index_id, kind, values=None):
        self.index_id, self.kind, self.values = index_id, kind, values
        self.connection = None

    def add(self, identity):
        if identity is None:
            return
        owner, order_id = identity
        if self.connection is not None:
            self.connection.execute(f'''INSERT INTO execution_plan_index_facts(index_id,owner,order_id,{self.kind})
                VALUES(%s,%s,%s,1) ON CONFLICT(index_id,owner,order_id)
                DO UPDATE SET {self.kind}=1,checked=0''', (self.index_id, owner, order_id))
            return
        with get_db_conn() as conn:
            conn.execute(f'''INSERT INTO execution_plan_index_facts(index_id,owner,order_id,{self.kind})
                VALUES(%s,%s,%s,1) ON CONFLICT(index_id,owner,order_id)
                DO UPDATE SET {self.kind}=1,checked=0''', (self.index_id, owner, order_id))
            conn.commit()

    def __contains__(self, identity):
        if identity is None:
            return False
        if self.connection is not None:
            return self.connection.execute(f'SELECT 1 FROM execution_plan_index_facts '
                                           f'WHERE index_id=%s AND owner=%s AND order_id=%s AND {self.kind}=1',
                                           (self.index_id, *identity)).fetchone() is not None
        with get_db_conn() as conn:
            return conn.execute(f'SELECT 1 FROM execution_plan_index_facts '
                                f'WHERE index_id=%s AND owner=%s AND order_id=%s AND {self.kind}=1',
                                (self.index_id, *identity)).fetchone() is not None

    def __iter__(self):
        if self.values is None:
            raise ValueError('Unbounded identity iteration is forbidden')
        return iter(self.values)


def index_id(ledger):
    return hashlib.sha256(json.dumps([ledger.scope, ledger.symbol, ledger.config_id]).encode()).hexdigest()


def index_status(ledger):
    with get_db_conn() as conn:
        row = conn.execute('SELECT phase,error,source_revision FROM execution_plan_index_runs WHERE index_id=%s',
                           (index_id(ledger),)).fetchone()
        revision = _revision(conn, ledger)
        pending = bool(_pending(conn, ledger, index_id(ledger), 1)) if not row else row['source_revision'] != revision
    complete = not pending and (not row or row['phase'] == 'complete')
    return {'complete': complete, 'ownership_ready': complete,
            'phase': row['phase'] if row else 'sources', 'processed': 0,
            'error': row['error'] if row else None}


def _pending(conn, ledger, key, limit):
    return conn.execute("""WITH candidates AS (
        SELECT s.*,r.owner AS prior_owner,r.relevant AS prior_relevant FROM ownership_sources s
        LEFT JOIN execution_plan_index_records r ON r.index_id=%s
            AND r.source=s.source AND r.source_id=s.source_id AND r.source_symbol=s.symbol
        WHERE s.symbol IN (%s,%s) AND (r.id IS NULL OR r.source_version<>s.version)
        ORDER BY s.source,s.source_id LIMIT %s), budgeted AS (
        SELECT *,sum(COALESCE(octet_length(payload),0)) OVER (ORDER BY source,source_id) AS bytes,
            row_number() OVER (ORDER BY source,source_id) AS n FROM candidates)
        SELECT source,source_id,symbol,owner,version,deleted,prior_owner,prior_relevant,
            CASE WHEN bytes<=4194304 THEN payload ELSE NULL END AS payload,
            COALESCE(octet_length(payload),0) AS payload_bytes
        FROM budgeted WHERE bytes<=4194304 OR n=1 ORDER BY source,source_id""",
        (key, ledger.symbol, ledger.symbol.split(':')[0], limit)).fetchall()


def _revision(conn, ledger):
    return conn.execute('SELECT COALESCE(SUM(revision),0) FROM plan_source_revisions '
                        'WHERE symbol IN (%s,%s)', (ledger.symbol,ledger.symbol.split(':')[0])).fetchone()[0]


def _facts(owner, plan, source):
    from backend.utils.exit_orders import is_conditional, parse_order_reference
    facts = {}
    def add(value, kind):
        if value is not None:
            raw = parse_order_reference(value)[1]
            fact = facts.setdefault(raw, dict(owner=owner, order_id=raw, regular=0, conditional=0))
            fact[kind] = 1
    if source == 'legacy':
        if str(plan.get('side') or '').upper() in {'BUY', 'SELL'} and plan.get('event_type') == 'ORDER_CREATED':
            add(plan.get('order_id'), 'regular')
    else:
        for item in plan.get('entries', []):
            add(item.get('id'), 'regular')
        add((plan.get('exit_order') or {}).get('id'), 'regular')
        for item in [*plan.get('legs', []), *plan.get('exits', [])]:
            add(item.get('id'), 'conditional' if plan.get('execution_mode') == 'independent_exits'
                and is_conditional(item) else 'regular')
            add(item.get('execution_order_id'), 'regular')
    return list(facts.values())


def _collect(conn, ledger, key, item):
    raw = item['payload']
    if raw is None and not item['deleted']:
        raise ValueError('Invalid ownership source')
    value = json.loads(raw) if raw else {}
    if not isinstance(value, dict):
        raise ValueError('Invalid ownership source')
    relevant = not item['deleted'] and bool(value) and (
        item['source'] == 'legacy' and item['owner'] == ledger.config_id
        or item['source'] != 'legacy' and (value.get('account_scope') == ledger.scope
            or not value.get('account_scope') and item['owner'] == ledger.config_id))
    # Same compact projection across thousands of snapshots is parsed/published once.
    fingerprint = hashlib.sha256(json.dumps([item['owner'], item['source'] == 'legacy', value],
                                sort_keys=True).encode()).hexdigest()
    if relevant:
        conn.execute("""INSERT INTO ownership_projections(index_id,fingerprint,owner,payload,facts)
            VALUES(%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING""",
            (key, fingerprint, item['owner'], raw, json.dumps(_facts(item['owner'], value, item['source']))))
    from backend.utils.execution_history_work import request_history
    if item.get('prior_relevant'):
        request_history(item['prior_owner'],ledger.scope,ledger.symbol,conn=conn)
    if relevant:
        request_history(item['owner'],ledger.scope,ledger.symbol,conn=conn)
    conn.execute("""INSERT INTO execution_plan_index_records
        (index_id,source,source_id,owner,fingerprint,payload,source_version,relevant,source_symbol)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(index_id,source,source_id,source_symbol) DO UPDATE
        SET owner=excluded.owner,fingerprint=excluded.fingerprint,payload=excluded.payload,
            source_version=excluded.source_version,relevant=excluded.relevant
        WHERE execution_plan_index_records.source_version<=excluded.source_version""",
        (key, item['source'], item['source_id'], item['owner'], fingerprint, '{}', item['version'], relevant,item['symbol']))


def _refresh_facts(conn, key):
    # Aggregate unique projections, not repeated raw snapshots. Retain checked on no-op.
    conn.execute("""WITH active AS (
        SELECT p.facts FROM ownership_projections p WHERE p.index_id=%s AND EXISTS
        (SELECT 1 FROM execution_plan_index_records r WHERE r.index_id=p.index_id
         AND r.fingerprint=p.fingerprint AND r.relevant)), facts AS (
        SELECT f.owner,f.order_id,max(f.regular) AS regular,max(f.conditional) AS conditional
        FROM active a CROSS JOIN LATERAL jsonb_to_recordset(a.facts)
          AS f(owner text,order_id text,regular int,conditional int) GROUP BY f.owner,f.order_id)
        INSERT INTO execution_plan_index_facts(index_id,owner,order_id,regular,conditional)
        SELECT %s,owner,order_id,regular,conditional FROM facts
        ON CONFLICT(index_id,owner,order_id) DO UPDATE SET regular=excluded.regular,
          conditional=excluded.conditional,checked=0
        WHERE execution_plan_index_facts.regular<>excluded.regular
          OR execution_plan_index_facts.conditional<>excluded.conditional""", (key,key))
    conn.execute("""DELETE FROM execution_plan_index_facts f WHERE f.index_id=%s AND NOT EXISTS (
        SELECT 1 FROM ownership_projections p CROSS JOIN LATERAL jsonb_to_recordset(p.facts)
            AS x(owner text,order_id text,regular int,conditional int)
        WHERE p.index_id=f.index_id AND x.owner=f.owner AND x.order_id=f.order_id
          AND EXISTS(SELECT 1 FROM execution_plan_index_records r
            WHERE r.index_id=p.index_id AND r.fingerprint=p.fingerprint AND r.relevant))""", (key,))


def _publication_piece(value, cursor):
    """Publish one order, never an unbounded loop under the source-version lock."""
    if 'event_type' in value:
        return value, True
    items = [(key, item) for key in ('entries','legs','exits') for item in value.get(key, [])]
    if value.get('exit_order'):
        items.append(('exit_order',value['exit_order']))
    piece = {key: item for key,item in value.items() if key not in {'entries','legs','exits','exit_order'}}
    if cursor < len(items):
        key, item = items[cursor]
        piece[key] = item if key == 'exit_order' else [item]
    return piece, cursor + 1 >= len(items)


def advance_index(ledger):
    from backend.utils.management_lock import scope_file_lock, ManagementLockBusy
    from backend.utils.execution_timing import stage
    try:
        with scope_file_lock('ownership-index', ledger.scope, ledger.symbol + '\0' + ledger.config_id):
            with stage('history_index'):
                return _advance_index(ledger)
    except ManagementLockBusy:
        return {'complete': False, 'ownership_ready': False, 'phase': 'busy', 'processed': 0}


def _advance_index(ledger):
    key = index_id(ledger)
    batch = positive_setting('RUNTIME_SCAN_BATCH_SIZE', 32)
    deadline = monotonic() + positive_setting('RUNTIME_INDEX_SCAN_SECONDS', .25, minimum=.001)
    byte_limit = 4 * 1024 * 1024
    processed, consumed, phase = 0, 0, 'sources'
    with get_db_conn() as conn:
        conn.execute("""INSERT INTO execution_plan_index_runs
            (index_id,account_scope,symbol,config_id,phase,cursor,incremental)
            VALUES(%s,%s,%s,%s,'sources',0,TRUE) ON CONFLICT DO NOTHING""",
            (key, ledger.scope, ledger.symbol, ledger.config_id))
        run = conn.execute('SELECT incremental,phase,source_revision FROM execution_plan_index_runs WHERE index_id=%s', (key,)).fetchone()
        if run['incremental'] and run['phase'] == 'complete' and run['source_revision'] == _revision(conn,ledger):
            return {'complete': True, 'ownership_ready': True, 'phase': 'complete', 'processed': 0, 'error': None}
        if not run['incremental']:
            conn.execute('DELETE FROM execution_plan_index_records WHERE index_id=%s', (key,))
            conn.execute('DELETE FROM execution_plan_index_facts WHERE index_id=%s', (key,))
            conn.execute('UPDATE execution_plan_index_runs SET incremental=TRUE WHERE index_id=%s', (key,))
        conn.commit()
    error = None
    try:
        # The slice deadline controls yielding between atomic work items. It
        # must not become a shrinking SQL timeout: a slow pending scan would
        # otherwise roll back the same batch forever.
        with get_db_conn() as conn:
            pending = _pending(conn, ledger, key, batch)
            for item in pending:
                if processed and (monotonic() >= deadline or consumed + len(item['payload'] or '') > byte_limit):
                    break
                if item['payload_bytes'] > byte_limit:
                    raise ValueError('Ownership source exceeds work budget')
                _collect(conn, ledger, key, item)
                processed += 1
                consumed += len((item['payload'] or '').encode())
            if processed:
                _refresh_facts(conn, key)
            conn.commit()
        # No publication while unprocessed evidence can change regular/algo identity.
        published = 0
        while processed < batch and (published == 0 or monotonic() < deadline):
            # Always attempt one publication after collecting sources, even if
            # the scan used the soft slice budget. Pending evidence still blocks
            # publication, preserving cross-page regular/algo disambiguation.
            with get_db_conn() as check:
                # Source writers take the corresponding revision row; this short
                # shared lock protects publication against newly committed sources.
                check.execute('SELECT revision FROM plan_source_revisions WHERE symbol IN (%s,%s) FOR SHARE',
                              (ledger.symbol, ledger.symbol.split(':')[0])).fetchall()
                if _pending(check, ledger, key, 1):
                    break
                fact = check.execute('SELECT id,owner,order_id FROM execution_plan_index_facts '
                                     'WHERE index_id=%s AND conditional=1 AND checked=0 LIMIT 1', (key,)).fetchone()
                if fact:
                    phase = 'aliases'
                    _, _, affected = ledger._retire_legacy_algo_aliases((), (),
                        conditional_ids=IdentitySet(key, 'conditional', [(fact['owner'],fact['order_id'])]),
                        regular_ids=IdentitySet(key, 'regular'))
                    check.execute('UPDATE execution_plan_index_facts SET checked=1 WHERE id=%s', (fact['id'],))
                    for owner in affected:
                        from backend.utils.execution_history_work import request_history
                        request_history(owner, ledger.scope, ledger.symbol, conn=check)
                else:
                    phase = 'publish'
                    row = check.execute("""SELECT p.* FROM ownership_projections p WHERE p.index_id=%s
                        AND NOT p.published AND EXISTS(SELECT 1 FROM execution_plan_index_records r
                        WHERE r.index_id=p.index_id AND r.fingerprint=p.fingerprint AND r.relevant) LIMIT 1""", (key,)).fetchone()
                    if not row:
                        phase = 'complete'
                        check.execute('UPDATE execution_plan_index_runs SET source_revision=%s WHERE index_id=%s',
                                      (_revision(check,ledger), key))
                        check.commit()
                        break
                    value, finished = _publication_piece(json.loads(row['payload']),row['publish_cursor'])
                    # Historical replay fills absent links; never overwrites current ACKs.
                    from backend.utils.execution_history_work import historical_publication, request_history
                    token = historical_publication.set(True)
                    try:
                        if 'event_type' in value:
                            ledger._index_legacy_row(value, IdentitySet(key,'conditional'), IdentitySet(key,'regular'))
                        else:
                            ledger._index_plan(row['owner'], value)
                    finally:
                        historical_publication.reset(token)
                    check.execute('UPDATE ownership_projections SET published=%s,publish_cursor=publish_cursor+1 '
                                  'WHERE index_id=%s AND fingerprint=%s', (finished,key,row['fingerprint']))
                    request_history(row['owner'], ledger.scope, ledger.symbol, conn=check)
                check.commit()
            processed += 1
            published += 1
    except (ValueError, TypeError) as exc:
        error = type(exc).__name__ + ': ownership source incomplete'
    with get_db_conn() as conn:
        conn.execute('UPDATE execution_plan_index_runs SET phase=%s,error=%s WHERE index_id=%s', (phase,error,key))
        conn.commit()
    return {'complete': phase == 'complete' and error is None,
            'ownership_ready': phase == 'complete' and error is None,
            'phase': phase, 'processed': processed, 'error': error}
