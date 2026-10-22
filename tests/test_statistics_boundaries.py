"""Reproductions for the follow-up statistics audit."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend import database
from backend.database.dca import DcaSnapshotStore
from backend.database.trade import TradeHistoryStore

pytestmark = pytest.mark.usefixtures('pg')


@pytest.mark.parametrize('mode', ['REAL', 'STRATEGY'])
@pytest.mark.parametrize('requested', ['BTC/USD', 'BTC/USD:BTC'])
def test_seven_day_statistics_do_not_mix_usd_and_usdt(mode, requested, monkeypatch):
    now = datetime.now(database.TZ_CN)
    stamp = (now - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
    ms = int((now - timedelta(hours=1)).timestamp()*1000)
    with database.get_db_conn() as conn:
        for i, symbol in enumerate(['BTC/USD:BTC', 'BTC/USDT:USDT']):
            if mode == 'REAL':
                conn.execute('''INSERT INTO execution_position_history(
                    position_id,account_scope,config_id,symbol,side,opened_at_ms,closed_at_ms,
                    entry_price,close_price,amount,realized_pnl,fees_json,exit_reason,updated_at_ms,payload)
                    VALUES(%s,'scope','audit',%s,'LONG',%s,%s,100,101,1,1,'{}','exit',%s,'{}')''',
                    (str(i), symbol, ms-1000, ms, ms))
            else:
                conn.execute('''INSERT INTO mock_orders(order_id,config_id,symbol,side,timestamp,
                    close_time,price,close_price,amount,realized_pnl,status)
                    VALUES(%s,'audit',%s,'BUY',%s,%s,100,101,1,1,'CLOSED')''', (str(i),symbol,stamp,stamp))
        conn.commit()
    if mode == 'REAL':
        from test_real_symbol_performance import seed_cycle
        monkeypatch.setattr('backend.utils.trade_evidence.configured_account_scope', lambda _: 'scope')
        for i, symbol in enumerate(['BTC/USD:BTC', 'BTC/USDT:USDT']):
            seed_cycle('scope', 'audit', symbol, ms-1000, ms, name=str(i), close_price=101, pnl=1)
    rows = database.get_closed_positions_7d('audit', requested, mode=mode)
    assert [r['symbol'] for r in rows] == ['BTC/USD:BTC']


def test_dca_chart_selects_latest_days_then_returns_chronological_order():
    snapshots = DcaSnapshotStore(database.get_db_conn, lambda: '', lambda: '')
    with database.get_db_conn() as conn:
        for day in range(1, 6):
            conn.execute('''INSERT INTO dca_daily_snapshots(snapshot_date,config_id,symbol,total_invested)
                VALUES(%s,'audit','BTC/USDT',%s)''', (f'2026-10-{day:02}', day*100))
        conn.commit()
    rows = snapshots.get_snapshot_history('audit', days=3, symbol='BTC/USDT')
    assert [r['snapshot_date'] for r in rows] == ['2026-10-03', '2026-10-04', '2026-10-05']


def test_trade_history_uses_application_timezone_on_utc_host(monkeypatch):
    from backend.database import trade as module
    class UTCServerDatetime(datetime):
        @classmethod
        def fromtimestamp(cls, timestamp, tz=None):
            return datetime.fromtimestamp(timestamp, tz=tz or timezone.utc)
    monkeypatch.setattr(module, 'datetime', UTCServerDatetime)
    TradeHistoryStore(database.get_db_conn, Mock()).save_trades([
        dict(id='tz', order='entry', symbol='BTC/USDT', side='buy', price=100, amount=1,
             cost=100, timestamp=int(datetime(2026,10,22,0,30,tzinfo=timezone.utc).timestamp()*1000))
    ], config_id='audit')
    with database.get_db_conn() as conn:
        row = conn.execute("SELECT timestamp FROM trade_history WHERE trade_id='tz'").fetchone()
    assert row['timestamp'] == '2026-10-22 08:30:00'


def test_spot_order_fill_time_uses_application_timezone_on_utc_host(monkeypatch):
    from backend.app.services import spot_dashboard_service as module
    class UTCServerDatetime(datetime):
        @classmethod
        def fromtimestamp(cls, timestamp, tz=None):
            return datetime.fromtimestamp(timestamp, tz=tz or timezone.utc)
    monkeypatch.setattr(module, 'datetime', UTCServerDatetime)
    with database.get_db_conn() as conn:
        conn.execute("""INSERT INTO orders(order_id,config_id,symbol,side,trade_mode,status)
            VALUES('tz-order','audit','BTC/USDT','BUY','SPOT_DCA','OPEN')""")
        conn.commit()
    timestamp = int(datetime(2026,10,22,20,30,tzinfo=timezone.utc).timestamp()*1000)
    exchange = SimpleNamespace(fetch_order=lambda *args: dict(id='tz-order',symbol='BTC/USDT',
        status='closed',filled=1,cost=100,average=100,lastTradeTimestamp=timestamp))
    errors = []
    dependencies = module.SpotStatisticsDependencies(
        connection_factory=database.get_db_conn, market_factory=Mock(),
        fill_writer=database.upsert_spot_order_fill, trade_writer=Mock(),
        snapshot_writer=Mock(), cache={}, cache_ttl=0)
    module._synchronize_orders('audit','BTC/USDT',SimpleNamespace(exchange=exchange),errors,dependencies)
    assert errors == []
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT filled_at FROM orders WHERE order_id='tz-order'").fetchone()[0] == '2026-10-23 04:30:00'
        assert conn.execute("SELECT filled_at FROM spot_order_fills WHERE order_id='tz-order'").fetchone()[0] == '2026-10-23 04:30:00'


def test_qualified_contract_statistics_keep_legacy_alias_but_exclude_other_contracts():
    stamp = (datetime.now(database.TZ_CN)-timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
    with database.get_db_conn() as conn:
        for index, symbol in enumerate(['BTC/USDT', 'BTC/USDT:USDT', 'BTC/USDT:USDT-261225']):
            conn.execute('''INSERT INTO mock_orders(order_id,config_id,symbol,side,timestamp,
                close_time,price,close_price,amount,realized_pnl,status)
                VALUES(%s,'audit',%s,'BUY',%s,%s,100,101,1,1,'CLOSED')''',
                (str(index),symbol,stamp,stamp))
        conn.commit()
    rows = database.get_closed_positions_7d('audit','BTC/USDT:USDT',mode='STRATEGY')
    assert {r['symbol'] for r in rows} == {'BTC/USDT','BTC/USDT:USDT'}
