import sqlite3
from unittest.mock import patch

import pytest

from backend.utils.performance_context import equity_metrics, performance_context
from backend.utils.sentiment_context import ratio_evidence
from backend.utils.market_data import MarketTool
from backend.utils.formatters import format_market_data_to_text


def test_equity_loss_recovery_and_drawdown():
    rows = [{'timestamp': str(i), 'total_equity': value}
            for i, value in enumerate([None, float('nan'), 0, 100, 80, 92])]
    result = equity_metrics(rows)
    assert result['return_pct'] == pytest.approx(-8)
    assert result['drawdown_pct'] == pytest.approx(8)
    assert result['max_drawdown_pct'] == pytest.approx(20)
    assert result['samples'] == 3
    assert not equity_metrics(rows[:4])['available']
    assert equity_metrics(rows + [{'timestamp': '7', 'total_equity': 0}])['return_pct'] == -100


def test_performance_is_strictly_scoped_and_uses_all_history():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE balance_history (id INTEGER, config_id TEXT, symbol TEXT, timestamp TEXT, total_equity REAL)')
    conn.executemany('INSERT INTO balance_history VALUES (?, ?, ?, ?, ?)', [
        (1, 'a', 'BTC/USDT', '2020-01-01', 100),
        (2, 'b', 'BTC/USDT', '2020-01-02', 1000),
        (3, 'a', 'ETH/USDT', '2020-01-03', 1),
        (4, 'a', 'BTC/USDT', '2026-01-01', 92),
    ])
    with patch('backend.database.get_db_conn', return_value=conn), patch('backend.database.get_closed_positions_7d', return_value=[]):
        text = performance_context('a', 'BTC/USDT', 'REAL')
        assert '-8.00%' in text
        assert '2020-01-01' in text
        assert '样本: 2' in text
        assert 'N/A' in performance_context('missing', 'BTC/USDT', 'REAL')
        assert performance_context('a', 'BTC/USDT', 'SPOT_DCA') == ''
    conn.close()


def test_ratio_freshness_missing_intervals_and_invalid_values():
    now = 1800000000000
    rows = [{'timestamp': now - 600000, 'longShortRatio': '2'},
            {'timestamp': now - 300000, 'longShortRatio': '3'}]
    result = ratio_evidence(list(reversed(rows)), 'longShortRatio', now)
    assert result['change_5m_pct'] == 50
    assert result['available']
    assert not ratio_evidence(rows, 'longShortRatio', now + 1000000)['available']
    assert ratio_evidence(rows[:1], 'longShortRatio', now)['change_5m_pct'] is None
    assert not ratio_evidence([{'timestamp': now, 'longShortRatio': 'nan'}], 'longShortRatio', now)['available']


def test_sentiment_partial_failure_preserves_other_sources():
    from unittest.mock import Mock
    tool = MarketTool.__new__(MarketTool)
    tool.exchange = Mock(id='binanceusdm')
    tool.exchange.market.return_value = {'id': 'BTCUSDT'}
    now = 1800000000000
    tool.exchange.fapiDataGetGlobalLongShortAccountRatio.return_value = [{'timestamp': now, 'longShortRatio': '1.2'}]
    tool.exchange.fapiDataGetTopLongShortPositionRatio.side_effect = RuntimeError('offline')
    tool.exchange.fapiDataGetTopLongShortAccountRatio.return_value = []
    tool.exchange.fapiDataGetTakerlongshortRatio.return_value = [{'timestamp': now, 'buySellRatio': '0.8'}]
    with patch('backend.utils.market_data.time.time', return_value=now / 1000):
        sentiment = tool._fetch_binance_specific_sentiment('BTC/USDT')
    assert sentiment['ls_accounts'] == 1.2
    assert sentiment['ls_ratio'] == 'N/A'
    assert sentiment['taker_buy_sell_ratio'] == .8
    text = format_market_data_to_text({'sentiment': sentiment})
    assert 'Taker buy/sell volume' in text
    assert 'fetch failed' in text
    assert 'timestamp_ms' in text
