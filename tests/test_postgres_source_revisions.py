"""Raw TEXT evidence must invalidate snapshots even when JSONB cannot decode it."""
import json

import pytest

from backend import database


def _payload(symbol='BTC/USDT:USDT', revision=1):
    return json.dumps({'account_scope': 'scope', 'symbol': symbol, 'side': 'LONG',
                       'revision': revision, 'memo': 'preserve\0raw',
                       'stop_loss': 50, 'take_profit': 150})


def _versions(conn):
    return {(row[0], row[1]): row[2] for row in conn.execute(
        'SELECT account_scope,symbol,revision FROM execution_source_revisions')}


@pytest.mark.parametrize('action', ['insert', 'update', 'delete'])
def test_unprojectable_event_mutation_invalidates_known_candidate_scopes(financial_pg, action):
    with database.get_db_conn() as conn:
        for scope, symbol in [('scope', 'BTC/USDT:USDT'), ('another', 'BTC/USDT'),
                              ('unrelated', 'ETH/USDT:USDT')]:
            conn.execute('INSERT INTO execution_source_revisions(account_scope,symbol,revision) VALUES(%s,%s,0)',
                         (scope, symbol))
        if action != 'insert':
            identity = conn.execute(
                'INSERT INTO real_protection_events(config_id,symbol,timestamp,payload) VALUES(%s,%s,%s,%s) RETURNING id',
                ('cfg', 'BTC/USDT', '2026-01-01 00:00:00', _payload())).fetchone()[0]
        conn.commit()
        before = _versions(conn)
        if action == 'insert':
            conn.execute('INSERT INTO real_protection_events(config_id,symbol,timestamp,payload) VALUES(%s,%s,%s,%s)',
                         ('cfg', 'BTC/USDT', '2026-01-01 00:00:00', _payload()))
        elif action == 'update':
            conn.execute('UPDATE real_protection_events SET payload=%s WHERE id=%s', (_payload(revision=2), identity))
        else:
            conn.execute('DELETE FROM real_protection_events WHERE id=%s', (identity,))
        after = _versions(conn)
        assert after['scope', 'BTC/USDT:USDT'] == before['scope', 'BTC/USDT:USDT'] + 1
        assert after['another', 'BTC/USDT'] == before['another', 'BTC/USDT'] + 1
        assert after['unrelated', 'ETH/USDT:USDT'] == before['unrelated', 'ETH/USDT:USDT']
        assert after.keys() == before.keys()
        conn.rollback()
        assert _versions(conn) == before


def test_raw_event_filters_and_bytes_survive_jsonb_projection_failure(financial_pg):
    from backend.utils.execution_cycles import _protection
    raw = _payload()
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_events(config_id,symbol,timestamp,payload) '
                     'VALUES(%s,%s,%s,%s)', ('cfg', 'BTC/USDT', '2026-01-01 00:00:00', raw))
        conn.commit()
        record = conn.execute('SELECT payload,storage_json(payload) FROM real_protection_events').fetchone()
        assert record[0] == raw and record[1] is None
        for scope, expected in [('scope', 1), ('another', 0)]:
            cycle = {'account_scope': scope, 'symbol': 'BTC/USDT:USDT', 'side': 'LONG',
                     'opened_at_ms': 0, 'closed_at_ms': 2000000000000}
            assert _protection(cycle, 'cfg', 4096, conn)
            assert len(cycle['protection_history']) == expected


def test_unprojectable_event_change_prevents_stale_history_publish(financial_pg, monkeypatch):
    from backend.utils import execution_ledger
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO execution_position_history(position_id,account_scope,config_id,symbol,payload) '
                     'VALUES(%s,%s,%s,%s,%s)', ('previous', 'scope', 'cfg', 'BTC/USDT:USDT', '{"complete":true}'))
        # _protection reads by config_id and applies payload scope/symbol in
        # Python. An inconsistent auxiliary symbol must not hide its mutation.
        conn.execute('INSERT INTO execution_order_links(account_scope,symbol,order_id,config_id,role,payload) '
                     'VALUES(%s,%s,%s,%s,%s,%s)', ('scope', 'BTC/USDT:USDT', 'entry', 'cfg', 'entry', '{}'))
        event_id = conn.execute('INSERT INTO real_protection_events(config_id,symbol,timestamp,payload) '
                                'VALUES(%s,%s,%s,%s) RETURNING id',
                                ('cfg', 'ETH/USDT', '2026-01-01 00:00:00', _payload())).fetchone()[0]
        conn.commit()

    def reconstruction(*args, **kwargs):
        with database.get_db_conn() as writer:
            writer.execute('UPDATE real_protection_events SET payload=%s WHERE id=%s',
                           (_payload(revision=2), event_id))
            writer.commit()
        return {'scan_complete': True}

    monkeypatch.setattr(execution_ledger, '_reconstruct_position_cycles', reconstruction)
    result = execution_ledger.rebuild_execution_position_history('cfg', 'scope', 'BTC/USDT:USDT')
    assert result['reason_code'] == 'cycle_source_changed' and result['retained_previous']
    assert not result['published']
    with database.get_db_conn() as conn:
        assert tuple(conn.execute('SELECT position_id,payload FROM execution_position_history').fetchone()) == (
            'previous', '{"complete":true}')
