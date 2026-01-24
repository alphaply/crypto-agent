import ccxt
import pandas as pd
import numpy as np
import os
from dotenv import load_dotenv
import time
import warnings
import database

warnings.filterwarnings("ignore")
load_dotenv()

class MarketTool:
    def __init__(self, proxy_port=None):
        """
        初始化交易所连接
        :param proxy_port: 本地代理端口 (例如 7890 或 10809), None 为直连
        """
        # 优先读取环境变量
        api_key = os.getenv('BINANCE_API_KEY')
        secret = os.getenv('BINANCE_SECRET')
        
        config = {
            'apiKey': api_key,
            'secret': secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'swap',
                'adjustForTimeDifference': True ,
                'recvWindow': 60000,
            }
        }
        
        # 如果传入了端口，或者想硬编码代理，可以在这里设置
        # 如果你在 .env 配置了 http_proxy 系统环境变量，ccxt 也会自动识别
        if proxy_port:
            config['proxies'] = {
                'http': f'http://127.0.0.1:{proxy_port}',
                'https': f'http://127.0.0.1:{proxy_port}',
            }
            
        self.exchange = ccxt.binanceusdm(config)
        
        # 建议：初始化时加载一次市场，触发时间校准 (虽然 lazy load 也会触发，但这样更稳)
        try:
            self.exchange.load_markets()
            print("✅ 交易所连接成功，时间已校准。")
        except Exception as e:
            print(f"⚠️ 初始化加载市场失败 (可能只有公共接口可用): {e}")

    # ==========================================
    # 0. 基础工具
    # ==========================================
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

    def _calculate_vp(self, df, length=360, rows=100, va_perc=0.70):
        if len(df) < length: return None
        subset = df.iloc[-length:].copy().reset_index(drop=True)
        high_val, low_val = subset['high'].max(), subset['low'].min()
        price_step = (high_val - low_val) / rows
        if price_step == 0: return None
        total_volume = np.zeros(rows)
        subset['start_slot'] = np.floor((subset['low'] - low_val) / price_step).astype(int).clip(0, rows - 1)
        subset['end_slot'] = np.floor((subset['high'] - low_val) / price_step).astype(int).clip(0, rows - 1)
        
        for row in subset.itertuples():
            level_low, level_high, level_vol = row.low, row.high, row.volume
            start_idx, end_idx = row.start_slot, row.end_slot
            for i in range(start_idx, end_idx + 1):
                p_level = low_val + i * price_step
                p_next = p_level + price_step
                proportion = 0.0
                if level_low >= p_level and level_high > p_next:
                    proportion = (p_next - level_low) / (level_high - level_low)
                elif level_high <= p_next and level_low < p_level:
                    proportion = (level_high - p_level) / (level_high - level_low)
                elif level_low >= p_level and level_high <= p_next:
                    proportion = 1.0
                else:
                    proportion = price_step / (level_high - level_low)
                total_volume[i] += level_vol * proportion

        poc_idx = np.argmax(total_volume)
        poc_price = low_val + (poc_idx + 0.5) * price_step
        total_traded = np.sum(total_volume)
        target = total_traded * va_perc
        curr, vah_i, val_i = total_volume[poc_idx], poc_idx, poc_idx
        while curr < target:
            if vah_i == rows - 1 and val_i == 0: break
            up = total_volume[vah_i + 1] if vah_i < rows - 1 else 0
            down = total_volume[val_i - 1] if val_i > 0 else 0
            if up == 0 and down == 0: break
            if up >= down:
                curr += up; vah_i += 1
            else:
                curr += down; val_i -= 1
        
        peak_n = int(rows * 0.09)
        peaks = []
        for i in range(rows):
            s_p, e_p = max(0, i - peak_n), min(rows, i + peak_n + 1)
            vol = total_volume[i]
            if vol == np.max(total_volume[s_p:e_p]) and vol > np.max(total_volume) * 0.01:
                peaks.append(low_val + (i + 0.5) * price_step)

        return {
            "poc": poc_price, 
            "vah": low_val + (vah_i + 1) * price_step, 
            "val": low_val + val_i * price_step,
            "hvns": sorted(peaks, reverse=True)
        }

    def _fetch_market_derivatives(self, symbol):
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            try:
                oi_data = self.exchange.fetch_open_interest(symbol)
                oi = float(oi_data.get('openInterestAmount', 0))
            except:
                oi = 0
                
            return {
                "funding_rate": float(ticker.get('info', {}).get('lastFundingRate', 0)),
                "open_interest": oi,
                "24h_quote_vol": float(ticker.get('quoteVolume', 0))
            }
        except Exception as e:
            print(f"Derivatives Error: {e}")
            return {"funding_rate": 0, "open_interest": 0, "24h_quote_vol": 0}

    # ==========================================
    # 1. 核心数据获取 (Agent & Dashboard 调用)
    # ==========================================
    def get_account_status(self, symbol):
        """
        获取账户全状态：
        1. balance: 可用保证金 (USDT)
        2. real_positions: 真实持仓
        3. real_open_orders: 已经在币安挂单但未成交的 (实盘)
        4. mock_open_orders: 本地数据库里的模拟单
        """
        try:
            # 1. 获取真实可用余额 (U本位合约)
            balance_info = self.exchange.fetch_balance()
            usdt_balance = float(balance_info.get('USDT', {}).get('free', 0))

            # 2. 获取真实持仓 (只读)
            all_positions = self.exchange.fetch_positions([symbol])
            real_positions = []
            for p in all_positions:
                if float(p['contracts']) > 0:
                    real_positions.append({
                        'symbol': p['symbol'],
                        'side': p['side'], # long/short
                        'amount': float(p['contracts']),
                        'entry_price': float(p['entryPrice']),
                        'unrealized_pnl': float(p['unrealizedPnl'])
                    })

            # 3. 核心增加：获取币安实盘挂单 (Open Orders)
            open_orders_raw = self.exchange.fetch_open_orders(symbol)
            real_open_orders = []
            for o in open_orders_raw:
                real_open_orders.append({
                    'order_id': o['id'],
                    'side': o['side'],
                    'price': o['price'],
                    'amount': o['amount'],
                    'type': o['type']
                })

            # 4. 获取模拟挂单 (从 SQLite)
            mock_orders = database.get_mock_orders(symbol)
            
            return {
                "balance": usdt_balance,
                "real_positions": real_positions,
                "real_open_orders": real_open_orders, # <--- 给 Agent 看到实盘 ID
                "mock_open_orders": mock_orders,
            }
        except Exception as e:
            print(f"❌ Account Status Error: {e}")
            return {
                "balance": 0, 
                "real_positions": [], 
                "real_open_orders": [], 
                "mock_open_orders": [], 
                "error": str(e)
            }

    def process_timeframe(self, symbol, tf):
        """处理单周期数据：计算全套 EMA、RSI、ATR 和 Volume Profile"""
        try:
            # 1. 获取 K 线 (Limit 1000 保证 EMA200 和 VP360 准确)
            ohlcv = self.exchange.fetch_ohlcv(symbol, tf, limit=1000)
            if not ohlcv: return None
            
            df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            df['time'] = pd.to_datetime(df['time'], unit='ms')
            
            close = df['close']
            volume = df['volume']
            
            # --- 2. 基础指标 (全套 EMA) ---
            # 计算 20, 50, 100, 200 均线，构建"均线排列"逻辑
            ema20 = self._calc_ema(close, 20).iloc[-1]
            ema50 = self._calc_ema(close, 50).iloc[-1]
            ema100 = self._calc_ema(close, 100).iloc[-1]
            ema200 = self._calc_ema(close, 200).iloc[-1]
            
            # 动量与波动率
            rsi = self._calc_rsi(close, 14).iloc[-1]
            atr = self._calc_atr(df, 14).iloc[-1]
            
            # --- 3. 成交量分析 (Volume Analysis) ---
            # 计算成交量均线 (20周期)，判断当前是否放量
            vol_ma20 = volume.rolling(window=20).mean().iloc[-1]
            current_vol = volume.iloc[-1]
            vol_ratio = round(current_vol / vol_ma20, 2) if vol_ma20 > 0 else 0
            
            # --- 4. Volume Profile (VP) ---
            # 使用之前定义的精确算法
            vp = self._calculate_vp(df, length=360)
            
            # 如果 VP 计算失败，给默认空值
            if not vp:
                vp = {"poc": 0, "vah": 0, "val": 0, "hvns": [], "lvns": []}

            # --- 5. 组装返回数据 ---
            return {
                "price": close.iloc[-1],
                
                # 动量与风险
                "rsi": round(rsi, 2),
                "atr": round(atr, 2),
                
                # 均线系统 (Agent 可以据此判断多头/空头排列)
                "ema": {
                    "ema_20": round(ema20, 2),   # 短期趋势
                    "ema_50": round(ema50, 2),   # 中期趋势
                    "ema_100": round(ema100, 2), # 强支撑/阻力
                    "ema_200": round(ema200, 2)  # 牛熊分界线
                },
                
                # 成交量状态
                "volume_analysis": {
                    "current": round(current_vol, 2),
                    "ma_20": round(vol_ma20, 2),
                    "ratio": vol_ratio,       # > 1.5 代表显著放量
                    "status": "High" if vol_ratio > 1.2 else "Low" # 简单状态描述
                },
                
                # 筹码分布 (VP)
                "vp": vp, 
                
                # 原始 DataFrame (用于 Dashboard 画图，Agent 不读这个)
                "df_raw": df 
            }
            
        except Exception as e:
            print(f"Process TF Error {tf}: {e}")
            return None

    def get_market_analysis(self, symbol):
        """主入口：获取指定币种的多周期数据 (15m, 1h, 4h, 1d)"""
        # 这里增加了 1d (日线)，对判断大趋势非常重要
        timeframes = ['15m', '1h', '4h', '1d']
        
        final_output = {
            "symbol": symbol,
            "timestamp": int(time.time()),
            "analysis": {},
            "sentiment": self._fetch_market_derivatives(symbol)
        }
        
        print(f"Fetching {symbol} market data...", end=" ", flush=True)
        
        for tf in timeframes:
            # print(f"[{tf}]", end=" ", flush=True) # 调试时可开启
            data = self.process_timeframe(symbol, tf)
            if data:
                final_output["analysis"][tf] = data
        
        print("Done.")     
        return final_output
    

    # market_data.py 的 MarketTool 类中增加以下方法


    def place_real_order(self, symbol, action, order_params):
        """
        实盘下单统一入口：修复撤单 ID 错误 (-1102) 与双向持仓参数冲突 (-1106)
        """
        try:
            self.exchange.load_markets()
            self.exchange.set_leverage(10, symbol)

            # 1. 撤单逻辑修复
            if action == 'CANCEL':
                order_id = order_params.get('cancel_order_id')
                if order_id and str(order_id).isdigit():
                    print(f"🚫 [REAL] 正在撤销实盘订单: {order_id}")
                    return self.exchange.cancel_order(order_id, symbol)
                else:
                    print(f"ℹ️ [REAL] 忽略模拟 ID 撤单请求: {order_id}")
                    return None

            # 2. 下单逻辑 (BUY_LIMIT / SELL_LIMIT)
            if action in ['BUY_LIMIT', 'SELL_LIMIT']:
                side = 'buy' if 'BUY' in action else 'sell'
                pos_side = 'LONG' if side == 'buy' else 'SHORT' # 适配双向持仓模式
                
                # 精度转换
                amount = float(self.exchange.amount_to_precision(symbol, order_params['amount']))
                price = float(self.exchange.price_to_precision(symbol, order_params['entry_price']))

                # A. 下开仓限价单
                print(f"🚀 [REAL] 发送 {side} {pos_side} 限价单: {amount} @ {price}")
                main_order = self.exchange.create_order(
                    symbol=symbol,
                    type='LIMIT',
                    side=side,
                    amount=amount,
                    price=price,
                    params={'timeInForce': 'GTC', 'positionSide': pos_side}
                )
                print(f"✅ 实盘主订单已创建 ID: {main_order['id']}")

                # B. 止盈止损逻辑 (注意：移除 reduceOnly 以适配双向持仓)
                reverse_side = 'sell' if side == 'buy' else 'buy'
                
                if order_params.get('stop_loss') > 0:
                    sl_p = float(self.exchange.price_to_precision(symbol, order_params['stop_loss']))
                    self.exchange.create_order(
                        symbol=symbol,
                        type='STOP_MARKET',
                        side=reverse_side,
                        amount=amount,
                        params={'stopPrice': sl_p, 'positionSide': pos_side}
                    )
                    print(f"🚩 实盘止损已挂出 @ {sl_p}")

                if order_params.get('take_profit') > 0:
                    tp_p = float(self.exchange.price_to_precision(symbol, order_params['take_profit']))
                    self.exchange.create_order(
                        symbol=symbol,
                        type='TAKE_PROFIT_MARKET',
                        side=reverse_side,
                        amount=amount,
                        params={'stopPrice': tp_p, 'positionSide': pos_side}
                    )
                    print(f"🎯 实盘止盈已挂出 @ {tp_p}")

                return main_order

        except Exception as e:
            print(f"❌ 实盘执行错误详情: {e}")
            # 不再 raise，防止一个币报错导致整个循环停止
            return None