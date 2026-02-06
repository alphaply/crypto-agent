import ccxt
import pandas as pd
import numpy as np
import os
from dotenv import load_dotenv
import time
import warnings
import database
from datetime import datetime
from utils.logger import setup_logger
import uuid
import math

logger = setup_logger("MarketData")
warnings.filterwarnings("ignore")
load_dotenv()

class MarketTool:
    def __init__(self, proxy_port=None):
        """
        初始化交易所连接
        :param proxy_port: 本地代理端口 (例如 7890 或 10809), None 为直连
        """
        api_key = os.getenv('BINANCE_API_KEY')
        secret = os.getenv('BINANCE_SECRET')
        
        config = {
            'apiKey': api_key,
            'secret': secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'swap',
                'adjustForTimeDifference': True,
                'recvWindow': 60000,
            }
        }
        
        if proxy_port:
            config['proxies'] = {
                'http': f'http://127.0.0.1:{proxy_port}',
                'https': f'http://127.0.0.1:{proxy_port}',
            }
            
        self.exchange = ccxt.binanceusdm(config)
        
        try:
            self.exchange.load_markets()
            logger.info("✅ 交易所连接成功，时间已校准。")
        except Exception as e:
            logger.warning(f"⚠️ 初始化加载市场失败: {e}")

    # ==========================================
    # 0. 基础工具 (指标计算与格式化)
    # ==========================================
    
    def _smart_fmt(self, value):
        """
        智能保留小数位，防止小币种数据被 round(x,2) 抹平
        """
        if value is None or pd.isna(value):
            return 0.0
        val = float(value)
        if val == 0: return 0.0
        
        abs_val = abs(val)
        if abs_val >= 1000:
            return round(val, 1)
        elif abs_val >= 1:
            return round(val, 3)
        elif abs_val >= 0.01:
            return round(val, 5)
        else:
            return round(val, 8) # 针对 PEPE 等超小币种

    def _calc_ema(self, series, span):
        return series.ewm(span=span, adjust=False).mean()

    def _calc_rsi(self, series, period=14):
        delta = series.diff()
        gain = (delta.where(delta > 0, 0))
        loss = (-delta.where(delta < 0, 0))
        avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _calc_atr(self, df, period=14):
        high, low, close = df['high'], df['low'], df['close']
        tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        return tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

    def _calc_macd(self, close, fast=12, slow=26, signal=9):
        """计算 MACD, Signal, Histogram"""
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    def _calc_bollinger_bands(self, close, window=20, num_std=2):
        """计算布林带"""
        rolling_mean = close.rolling(window=window).mean()
        rolling_std = close.rolling(window=window).std()
        upper = rolling_mean + (rolling_std * num_std)
        lower = rolling_mean - (rolling_std * num_std)
        return upper, rolling_mean, lower

    def _calc_kdj(self, df, n=9, m1=3, m2=3):
        """计算 KDJ 指标"""
        low_list = df['low'].rolling(n).min()
        high_list = df['high'].rolling(n).max()
        rsv = (df['close'] - low_list) / (high_list - low_list) * 100
        # Pandas ewm 模拟 SMA 递归
        k = rsv.ewm(alpha=1/m1, adjust=False).mean()
        d = k.ewm(alpha=1/m2, adjust=False).mean()
        j = 3 * k - 2 * d
        return k, d, j

    def _calculate_vp(self, df, length=360, rows=100, va_perc=0.70):
        """
        计算体积分布 (Volume Profile) - 优化版
        """
        if len(df) < 50: return None
        
        subset = df.iloc[-length:].copy().reset_index(drop=True)
        high_val = subset['high'].max()
        low_val = subset['low'].min()
        
        if high_val == low_val: return None
        
        price_step = (high_val - low_val) / rows
        total_volume = np.zeros(rows)
        
        highs = subset['high'].values
        lows = subset['low'].values
        vols = subset['volume'].values
        
        # 向量化分配成交量
        for i in range(len(subset)):
            h, l, v = highs[i], lows[i], vols[i]
            if h == l:
                bin_idx = min(int((h - low_val) / price_step), rows - 1)
                total_volume[bin_idx] += v
                continue
            
            start_bin = max(0, min(int((l - low_val) / price_step), rows - 1))
            end_bin = max(0, min(int((h - low_val) / price_step), rows - 1))
            vol_per_price = v / (h - l)
            
            for b in range(start_bin, end_bin + 1):
                bin_low = low_val + b * price_step
                bin_high = low_val + (b + 1) * price_step
                overlap = max(0, min(h, bin_high) - max(l, bin_low))
                total_volume[b] += overlap * vol_per_price

        # POC
        poc_idx = np.argmax(total_volume)
        poc_price = low_val + (poc_idx + 0.5) * price_step
        
        # VA (Value Area)
        total_traded_vol = np.sum(total_volume)
        target_vol = total_traded_vol * va_perc
        current_vol = total_volume[poc_idx]
        vah_idx = val_idx = poc_idx
        
        while current_vol < target_vol:
            if vah_idx >= rows - 1 and val_idx <= 0: break
            up_vol = total_volume[vah_idx + 1] if vah_idx < rows - 1 else 0
            down_vol = total_volume[val_idx - 1] if val_idx > 0 else 0
            
            if up_vol >= down_vol:
                vah_idx += 1; current_vol += up_vol
            else:
                val_idx -= 1; current_vol += down_vol
                
        vah_price = low_val + (vah_idx + 1) * price_step
        val_price = low_val + val_idx * price_step
        
        # HVN (筹码峰)
        hvns = []
        window = max(1, int(rows * 0.05)) 
        for i in range(window, rows - window):
            current_val = total_volume[i]
            if current_val > np.max(total_volume) * 0.1: # 过滤噪点
                # 检查局部最大值
                if all(current_val >= total_volume[i-window:i]) and all(current_val >= total_volume[i+1:i+1+window]):
                    hvns.append(low_val + (i + 0.5) * price_step)
        
        if not hvns: hvns.append(poc_price)

        return {
            "poc": self._smart_fmt(poc_price), 
            "vah": self._smart_fmt(vah_price), 
            "val": self._smart_fmt(val_price),
            "hvns": [self._smart_fmt(x) for x in sorted(hvns, reverse=True)]
        }

    def _fetch_market_derivatives(self, symbol):
        """获取资金费率、持仓量等衍生品数据"""
        try:
            funding_rate = 0
            try:
                fr_data = self.exchange.fetch_funding_rate(symbol)
                funding_rate = float(fr_data.get('fundingRate', 0))
            except:
                # 备用方法
                ticker = self.exchange.fetch_ticker(symbol)
                funding_rate = float(ticker.get('info', {}).get('lastFundingRate', 0))

            try:
                oi_data = self.exchange.fetch_open_interest(symbol)
                oi = float(oi_data.get('openInterestAmount', 0))
            except:
                oi = 0
                
            ticker = self.exchange.fetch_ticker(symbol)
            quote_vol = float(ticker.get('quoteVolume', 0))
                
            return {
                "funding_rate": funding_rate,
                "open_interest": oi,
                "24h_quote_vol": quote_vol
            }
        except Exception as e:
            logger.error(f"Derivatives Error: {e}")
            return {"funding_rate": 0, "open_interest": 0, "24h_quote_vol": 0}

    # ==========================================
    # 1. 获取数据逻辑
    # ==========================================

    def get_account_status(self, symbol, is_real=False, agent_name=None):
        status_data = {
            "balance": 0,
            "real_positions": [],
            "real_open_orders": [],
            "mock_open_orders": [],
        }
        
        try:
            if is_real:
                balance_info = self.exchange.fetch_balance()
                status_data["balance"] = float(balance_info.get('USDT', {}).get('free', 0))
                
                # 实盘持仓
                all_positions = self.exchange.fetch_positions([symbol])
                real_positions = [
                    {
                        'symbol': p['symbol'],
                        'side': p['side'],
                        'amount': float(p['contracts']),
                        'entry_price': float(p['entryPrice']),
                        'unrealized_pnl': float(p['unrealizedPnl'])
                    } for p in all_positions if float(p['contracts']) > 0
                ]
                status_data["real_positions"] = real_positions

                # 实盘挂单
                try:
                    all_orders = self.exchange.fetch_open_orders(symbol)
                    filtered_orders = []
                    for o in all_orders:
                        filtered_orders.append({
                            'order_id': str(o.get('id')),
                            'side': o.get('side', '').lower(),
                            'type': o.get('type'),
                            'price': float(o.get('price') or 0),
                            'amount': float(o.get('amount', 0)),
                            'status': o.get('status')
                        })
                    status_data["real_open_orders"] = filtered_orders
                except Exception as e:
                    logger.warning(f"Fetch real orders error: {e}")
            else:
                # 模拟模式
                status_data["balance"] = 10000.0 
                status_data["mock_open_orders"] = database.get_mock_orders(symbol, agent_name=agent_name)
                
        except Exception as e:
            logger.error(f"Account Status Error: {e}")
        
        return status_data

    def get_market_analysis(self, symbol, mode='STRATEGY'):
        """
        全量获取市场数据的主入口
        """
        if mode == 'REAL':
            timeframes = ['5m', '15m', '1h', '4h']
        else:
            timeframes = ['15m', '1h', '4h', '1d', '1w']

        final_output = {
            "symbol": symbol,
            "timestamp": int(time.time()),
            "analysis": {},
            "sentiment": self._fetch_market_derivatives(symbol)
        }
        
        logger.info(f"Fetching {symbol} market data ({mode} mode: {timeframes})...")
        
        for tf in timeframes:
            # 确保传递了 symbol 和 tf
            data = self.process_timeframe(symbol, tf)
            if data:
                final_output["analysis"][tf] = data
        
        return final_output

    def process_timeframe(self, symbol, tf):
        """
        处理单个时间周期的核心逻辑（含指标计算升级）
        """
        try:
            # 1. 获取 OHLCV
            # limit 适当调大，保证 EMA/MACD 计算准确
            ohlcv = self.exchange.fetch_ohlcv(symbol, tf, limit=500)
            if not ohlcv or len(ohlcv) < 50: return None
            
            df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            df['time'] = pd.to_datetime(df['time'], unit='ms')
            
            # 提取 Series
            close = df['close']
            high = df['high']
            low = df['low']
            volume = df['volume']
            
            # ================= 计算指标 =================
            # 1. 基础均线
            ema20 = self._calc_ema(close, 20)
            ema50 = self._calc_ema(close, 50)
            ema100 = self._calc_ema(close, 100)
            ema200 = self._calc_ema(close, 200)
            
            # 2. 动量与震荡
            rsi = self._calc_rsi(close, 14)
            atr = self._calc_atr(df, 14)
            macd, signal, hist = self._calc_macd(close)
            k, d, j = self._calc_kdj(df)
            
            # 3. 布林带 (判断波动率挤压)
            bb_up, bb_mid, bb_low = self._calc_bollinger_bands(close)
            bb_width = (bb_up - bb_low) / bb_mid # 带宽
            
            # 4. 成交量分析
            vol_ma20 = volume.rolling(window=20).mean()
            vol_ratio = (volume / vol_ma20).fillna(0)
            
            # 5. VP 分布
            vp = self._calculate_vp(df, length=360)
            if not vp: vp = {"poc": 0, "vah": 0, "val": 0, "hvns": []}
            
            # ================= 提取最新值 =================
            # 使用 iloc[-1] 获取最新的一根K线（可能是未完成的）
            # 注意：对于策略判断，通常看已完成的 (-2)，但这里返回最新状态供 Agent 参考
            
            curr_close = close.iloc[-1]
            
            # 趋势判定逻辑 (简单版)
            trend_status = "Consolidation"
            e20_val = ema20.iloc[-1]
            e50_val = ema50.iloc[-1]
            e200_val = ema200.iloc[-1]
            if e20_val > e50_val > e200_val: trend_status = "Uptrend"
            elif e20_val < e50_val < e200_val: trend_status = "Downtrend"
            
            # ================= 序列数据提取 (Fix: 强制 float 转换) =================
            # 取最近5根（包含当前未完成的）
            def to_list(series, n=5):
                # iloc切片 -> values -> tolist -> map(float) -> smart_fmt
                raw = series.iloc[-n:].values.tolist()
                return [self._smart_fmt(float(x)) for x in raw]

            recent_closes = to_list(close)
            recent_highs = to_list(high)
            recent_lows = to_list(low)
            
            return {
                "price": self._smart_fmt(curr_close),
                "trend_status": trend_status, # 新增
                "recent_closes": recent_closes,
                "recent_highs": recent_highs, # Fix: 确保有值
                "recent_lows": recent_lows,   # Fix: 确保有值
                
                # 震荡指标
                "rsi": round(float(rsi.iloc[-1]), 1),
                "kdj": {
                    "k": round(float(k.iloc[-1]), 1),
                    "d": round(float(d.iloc[-1]), 1),
                    "j": round(float(j.iloc[-1]), 1)
                },
                
                # 波动率与动能
                "atr": self._smart_fmt(atr.iloc[-1]),
                "macd": {
                    "diff": self._smart_fmt(macd.iloc[-1]),
                    "dea": self._smart_fmt(signal.iloc[-1]),
                    "hist": self._smart_fmt(hist.iloc[-1])
                },
                "bollinger": {
                    "up": self._smart_fmt(bb_up.iloc[-1]),
                    "mid": self._smart_fmt(bb_mid.iloc[-1]),
                    "low": self._smart_fmt(bb_low.iloc[-1]),
                    "width": round(float(bb_width.iloc[-1]), 4)
                },

                # 趋势均线
                "ema": {
                    "ema_20": self._smart_fmt(e20_val),
                    "ema_50": self._smart_fmt(e50_val),
                    "ema_100": self._smart_fmt(ema100.iloc[-1]),
                    "ema_200": self._smart_fmt(e200_val)
                },
                
                # 量能
                "volume_analysis": {
                    "current": self._smart_fmt(volume.iloc[-1]),
                    "ratio": round(float(vol_ratio.iloc[-1]), 2),
                    "status": "High" if float(vol_ratio.iloc[-1]) > 1.5 else ("Low" if float(vol_ratio.iloc[-1]) < 0.5 else "Normal")
                },
                
                # 筹码分布
                "vp": vp
            }
        except Exception as e:
            logger.error(f"Process TF Error {tf}: {e}")
            import traceback
            traceback.print_exc()
            return None

    # ==========================================
    # 实盘下单逻辑 (保持不变或微调)
    # ==========================================
    def place_real_order(self, symbol, action, order_params, agent_name=None):
        try:
            if not self.exchange.markets: self.exchange.load_markets()
            symbol = str(symbol)
            
            if action == 'CANCEL':
                cancel_id = order_params.get('cancel_order_id')
                if cancel_id:
                    self.exchange.cancel_order(cancel_id, symbol)
                    return {"status": "cancelled"}
                return None

            if action == 'CLOSE':
                # 简化版平仓逻辑
                raw_close_amount = float(order_params.get('amount', 0))
                positions = self.exchange.fetch_positions([symbol])
                for pos in positions:
                    amt = float(pos['contracts'])
                    if amt > 0:
                        side = pos['side']
                        close_side = 'sell' if side == 'long' else 'buy'
                        # 如果指定数量且小于持仓，则部分平；否则全平
                        final_amt = raw_close_amount if (0 < raw_close_amount < amt) else amt
                        
                        self.exchange.create_order(symbol, 'MARKET', close_side, final_amt, params={'positionSide': 'LONG' if side == 'long' else 'SHORT'})
                return {"status": "closed"}

            if action in ['BUY_LIMIT', 'SELL_LIMIT']:
                side = 'buy' if 'BUY' in action else 'sell'
                pos_side = 'LONG' if side == 'buy' else 'SHORT'
                amount = self.exchange.amount_to_precision(symbol, float(order_params['amount']))
                price = self.exchange.price_to_precision(symbol, float(order_params['entry_price']))
                
                params = {'timeInForce': 'GTC', 'positionSide': pos_side}
                order = self.exchange.create_order(symbol, 'LIMIT', side, amount, price, params=params)
                return order

        except Exception as e:
            logger.error(f"Order Error: {e}")
            return None
            
    def fetch_recent_trades(self, symbol, limit=20):
        try:
            return self.exchange.fetch_my_trades(symbol, limit=limit)
        except:
            return []