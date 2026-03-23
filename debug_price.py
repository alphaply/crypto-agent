import ccxt
import pandas as pd
from datetime import datetime, timezone, timedelta
import pytz

exchange = ccxt.binanceusdm()
exchange.load_markets()
symbol = 'BTC/USDT:USDT' 
timeframe = '1m'

# Beijing time is UTC+8
tz_beijing = pytz.timezone('Asia/Shanghai')

# Target 21:55 Beijing time
dt_beijing = tz_beijing.localize(datetime(2026, 3, 23, 21, 55, 0))
since = int(dt_beijing.timestamp() * 1000)

ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=15)
df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
df['time_beijing'] = pd.to_datetime(df['timestamp'], unit='ms').dt.tz_localize('UTC').dt.tz_convert('Asia/Shanghai')

print(df[['time_beijing', 'open', 'high', 'low', 'close']])
