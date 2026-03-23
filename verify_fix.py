import sys
from unittest.mock import MagicMock, patch
import pandas as pd
from datetime import datetime
import pytz

# Mock dependencies
sys.modules['ccxt'] = MagicMock()
sys.modules['database'] = MagicMock()
sys.modules['utils.logger'] = MagicMock()
sys.modules['config'] = MagicMock()

import database
database.TZ_CN = pytz.timezone('Asia/Shanghai')

# Import MarketTool AFTER mocking
from utils.market_data import MarketTool

def test_fix():
    # Setup mock tool
    with patch.object(MarketTool, '__init__', return_value=None):
        tool = MarketTool()
        tool.config_id = "test_config"
        tool.symbol = "BTC/USDT"
        tool.exchange = MagicMock()
        
        # Order created at 21:00 Beijing (13:00 UTC)
        # Beijing is UTC+8
        order_time_literal = '2026-03-23 21:00:00'
        
        # Mock order
        mock_order = {
            'order_id': 'TEST-O1',
            'timestamp': order_time_literal,
            'side': 'SELL',
            'price': 71300.0,
            'amount': 0.1,
            'stop_loss': 72000.0,
            'take_profit': 69500.0,
            'is_filled': 1,
            'status': 'OPEN'
        }
        
        database.get_mock_orders.return_value = [mock_order]
        
        # Test Case 1: process_timeframe('1d')
        # This used to call _check_mock_orders_tp_sl and trigger TP.
        # Now it shouldn't call it at all.
        df_1d = pd.DataFrame({
            'high': [71000, 71500],
            'low': [70800, 67000],
            'open': [70900, 71400],
            'close': [71200, 68000],
            'volume': [100, 200]
        })
        
        with patch('pandas.DataFrame', return_value=df_1d):
            with patch.object(tool, '_check_mock_orders_tp_sl') as mock_check:
                tool.exchange.fetch_ohlcv.return_value = [[0, 71400, 71500, 67000, 68000, 200]]
                print("Testing process_timeframe('1d')...")
                try: tool.process_timeframe("BTC/USDT", "1d")
                except: pass
                
                if mock_check.called:
                    print("FAIL: _check_mock_orders_tp_sl was called during process_timeframe!")
                else:
                    print("SUCCESS: _check_mock_orders_tp_sl was NOT called during process_timeframe.")

        # Test Case 2: run_silent_sl_tp() at 22:00
        # 22:00 Beijing = 14:00 UTC = 1774360800000 ms
        target_time_ms = 1774360800000
        
        # Safe 1m candle (High 71500, Low 71400)
        ohlcv_safe = [[target_time_ms, 71450, 71500, 71400, 71480, 10]]
        tool.exchange.fetch_ohlcv.return_value = ohlcv_safe
        
        print("\nTesting run_silent_sl_tp() at 22:00 with price=71400 (Safe)...")
        tool.run_silent_sl_tp()
        if database.close_mock_order.called:
            print("FAIL: close_mock_order was called incorrectly!")
        else:
            print("SUCCESS: close_mock_order was NOT called.")

        database.close_mock_order.reset_mock()

        # Test Case 3: run_silent_sl_tp() at 22:01 with TP hit
        # Low = 69000 (TP is 69500)
        ohlcv_tp = [[target_time_ms + 60000, 71450, 71500, 69000, 69100, 10]]
        tool.exchange.fetch_ohlcv.return_value = ohlcv_tp
        
        print("\nTesting run_silent_sl_tp() at 22:01 with price=69000 (TP Hit!)...")
        tool.run_silent_sl_tp()
        if database.close_mock_order.called:
            print("SUCCESS: close_mock_order WAS called when 1m price hit TP.")
        else:
            print("FAIL: close_mock_order was NOT called when 1m price hit TP!")

if __name__ == "__main__":
    test_fix()
