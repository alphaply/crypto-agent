import ccxt
import pandas as pd
import numpy as np
import os
from dotenv import load_dotenv
import time
import warnings
import database
from datetime import datetime

warnings.filterwarnings("ignore")
load_dotenv()

class MarketTool:
    def __init__(self, proxy_port=None):
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
            config['proxies'] = {'http': f'http://127.0.0.1:{proxy_port}', 'https': f'http://127.0.0.1:{proxy_port}'}
            
        self.exchange = ccxt.binanceusdm(config)
        try:
            self.exchange.load_markets()
            print("✅ 交易所连接成功")
        except Exception as e:
            print(f"⚠️ 初始化失败: {e}")

    # ==========================================
    # 0. 基础指标计算
    # ==========================================
    def _calc_ema(self, series, span): return series.ewm(span=span, adjust=False).mean()
    
    def _calc_rsi(self, series, period=14):
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        return 100 - (100 / (1 + gain/loss))

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
        subset['start'] = np.floor((subset['low'] - low_val) / price_step).astype(int).clip(0, rows - 1)
        subset['end'] = np.floor((subset['high'] - low_val) / price_step).astype(int).clip(0, rows - 1)
        
        for row in subset.itertuples():
            if row.end >= row.start:
                vol_per_slot = row.volume / (row.end - row.start + 1)
                total_volume[row.start : row.end + 1] += vol_per_slot
            
        poc_idx = np.argmax(total_volume)
        poc_price = low_val + (poc_idx + 0.5) * price_step
        
        # 简单计算峰值 (High Volume Nodes)
        peaks = []
        peak_window = 3 # 窗口越小越灵敏
        for i in range(peak_window, rows - peak_window):
             if total_volume[i] == max(total_volume[i-peak_window:i+peak_window]) and total_volume[i] > np.mean(total_volume) * 1.5:
                 peaks.append(low_val + (i + 0.5) * price_step)
        
        return {"poc": poc_price, "vah": 0, "val": 0, "hvns": sorted(peaks, reverse=True)[:5]}

    def _fetch_market_derivatives(self, symbol):
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return {
                "funding_rate": float(ticker.get('info', {}).get('lastFundingRate', 0)),
                "open_interest": float(self.exchange.fetch_open_interest(symbol).get('openInterestAmount', 0)),
                "24h_quote_vol": float(ticker.get('quoteVolume', 0))
            }
        except: return {"funding_rate": 0, "open_interest": 0, "24h_quote_vol": 0}

    # ==========================================
    # 1. 获取数据逻辑 (区分实盘与策略周期)
    # ==========================================

    def get_account_status(self, symbol, is_real=False):
        status = {"balance": 0, "real_positions": [], "real_open_orders": [], "mock_open_orders": []}
        if is_real:
            try:
                bal = self.exchange.fetch_balance()
                status["balance"] = float(bal.get('USDT', {}).get('free', 0))
                
                # 持仓
                positions = self.exchange.fetch_positions([symbol])
                status["real_positions"] = [
                    {'symbol': p['symbol'], 'side': p['side'], 'amount': float(p['contracts']), 
                     'entry_price': float(p['entryPrice']), 'unrealized_pnl': float(p['unrealizedPnl'])}
                    for p in positions if float(p['contracts']) > 0
                ]
                
                # 订单 (fetch_open_orders 通常包含 STOP_MARKET/TAKE_PROFIT 等)
                # 显式转换 symbol 格式以防万一
                orders = self.exchange.fetch_open_orders(symbol)
                for o in orders:
                    raw = o.get('info', {})
                    
                    # 价格解析：如果是 LIMIT 单，取 price；如果是 STOP 单，取 stopPrice
                    price_val = float(o.get('price') or 0)
                    trigger_val = float(o.get('stopPrice') or raw.get('stopPrice') or raw.get('activatePrice') or 0)
                    
                    # 如果是 STOP_MARKET，API返回的 price 可能是 0，但 trigger_val 有值
                    # 我们统一把“挂单价”展示为 trigger_val (如果是条件单)
                    display_price = price_val if price_val > 0 else trigger_val

                    status["real_open_orders"].append({
                        'order_id': str(o.get('id')),
                        'side': o.get('side'),
                        'type': o.get('type'), # LIMIT, STOP_MARKET, etc.
                        'price': display_price, 
                        'trigger_price': trigger_val,
                        'amount': float(o.get('amount', 0)),
                        'reduce_only': bool(raw.get('reduceOnly', False))
                    })
            except Exception as e:
                print(f"⚠️ [API Error] {e}")
        else:
            status["mock_open_orders"] = database.get_mock_orders(symbol)
            status["balance"] = 10000.0
            
        return status

    def get_market_analysis(self, symbol, mode="STRATEGY"):
        """
        根据模式获取不同周期的市场数据
        mode: "REAL" | "STRATEGY"
        """
        # 定义不同模式关注的周期
        if mode == "REAL":
            # 实盘：侧重短线细节 + 4H 趋势
            timeframes = ['5m', '15m', '1h', '4h'] 
        else:
            # 策略：侧重中长线结构 (4H, 12H, 1D, 3D, 1W)
            timeframes = ['4h', '12h', '1d', '3d', '1w']

        final_output = {
            "symbol": symbol,
            "mode": mode,
            "timestamp": int(time.time()),
            "analysis": {},
            "sentiment": self._fetch_market_derivatives(symbol)
        }
        
        print(f"Fetching {symbol} market data (Mode: {mode}, TFs: {timeframes})...", end=" ", flush=True)
        
        for tf in timeframes:
            # 3d 和 1w 这种大周期可能需要更长的等待或不同的 limit，这里统一 limit=500
            data = self.process_timeframe(symbol, tf)
            if data:
                final_output["analysis"][tf] = data
        
        print("Done.")     
        return final_output

    def process_timeframe(self, symbol, tf):
        try:
            # 针对大周期稍微减少数据量以防超时，针对小周期增加数据量计算 VP
            limit = 500 if tf in ['1w', '3d'] else 1000
            
            ohlcv = self.exchange.fetch_ohlcv(symbol, tf, limit=limit)
            if not ohlcv or len(ohlcv) < 50: return None
            
            df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            close = df['close']
            
            # 动态计算 EMA，确保数据长度足够
            ema_dict = {}
            for span in [20, 50, 100, 200]:
                if len(close) > span:
                    ema_dict[f"ema_{span}"] = round(self._calc_ema(close, span).iloc[-1], 2)
                else:
                    ema_dict[f"ema_{span}"] = 0

            vp = self._calculate_vp(df)
            if not vp: vp = {"poc": 0, "vah": 0, "val": 0, "hvns": []}

            return {
                "price": close.iloc[-1],
                "rsi": round(self._calc_rsi(close).iloc[-1], 2),
                "atr": round(self._calc_atr(df).iloc[-1], 2),
                "ema": ema_dict,
                "vp": vp,
                "volume_analysis": {"status": "Normal"}
            }
        except Exception as e: 
            print(f"⚠️ Process TF {tf} Error: {e}")
            return None

    # ==========================================
    # 2. 实盘核心下单逻辑 (修改：TP 为 Limit, SL 为 StopMarket)
    # ==========================================

    def place_real_order(self, symbol, action, order_params):
        try:
            if not self.exchange.markets: self.exchange.load_markets()
            symbol = str(symbol)
            
            # --- 1. 撤单逻辑 ---
            if action == 'CANCEL':
                cancel_id = order_params.get('cancel_order_id')
                if cancel_id:
                    print(f"🔄 [REAL] 撤单: {cancel_id}")
                    try:
                        self.exchange.cancel_order(cancel_id, symbol)
                        return {"status": "cancelled", "id": cancel_id}
                    except Exception as e:
                        print(f"❌ 撤单失败: {e}")
                return None

            # --- 2. 平仓逻辑 (Close All) ---
            if action == 'CLOSE':
                print(f"⚠️ [REAL] 执行全平...")
                try:
                    self.exchange.cancel_all_orders(symbol) # 先撤挂单
                    positions = self.exchange.fetch_positions([symbol])
                    for pos in positions:
                        amt = float(pos['contracts'])
                        if amt > 0:
                            # 无论 Hedge 还是 One-way，平仓都是反向开单
                            side = 'sell' if pos['side'] == 'long' else 'buy'
                            params = {'positionSide': 'LONG' if pos['side'] == 'long' else 'SHORT'}
                            self.exchange.create_order(symbol, 'MARKET', side, amt, params=params)
                            print(f"   |-- ✅ 平仓 {pos['side']} {amt}")
                    return {"status": "closed"}
                except Exception as e:
                    print(f"❌ 平仓失败: {e}")
                return None

            # --- 3. 开仓 (BUY_LIMIT / SELL_LIMIT) ---
            if action in ['BUY_LIMIT', 'SELL_LIMIT']:
                side = 'buy' if 'BUY' in action else 'sell'
                pos_side = 'LONG' if side == 'buy' else 'SHORT' # 双向持仓模式逻辑
                
                amount = float(self.exchange.amount_to_precision(symbol, order_params['amount']))
                price = float(self.exchange.price_to_precision(symbol, order_params['entry_price']))

                params = {'timeInForce': 'GTC', 'positionSide': pos_side}
                
                print(f"🚀 [REAL] 开仓限价单: {side} {amount} @ {price}")
                return self.exchange.create_order(symbol, 'LIMIT', side, amount, price, params=params)

            # --- 4. 持仓管理 (ADD_TP / ADD_SL) [重点修改] ---
            if action in ['ADD_TP', 'ADD_SL']:
                # 必须先获取当前持仓方向
                positions = [p for p in self.exchange.fetch_positions([symbol]) if float(p['contracts']) > 0]
                if not positions:
                    print("❌ 无法添加 TP/SL: 当前无持仓")
                    return None
                
                # 假设针对最大持仓进行操作 (主力仓位)
                main_pos = max(positions, key=lambda x: float(x['contracts']))
                is_long = (main_pos['side'] == 'long')
                
                # 决定下单方向：多单止盈/止损是卖出，空单是买入
                order_side = 'sell' if is_long else 'buy'
                position_side = 'LONG' if is_long else 'SHORT'
                
                trigger_price = float(self.exchange.price_to_precision(symbol, order_params['entry_price']))
                
                # 数量：如果没有指定 amount，默认处理全部持仓
                req_amount = float(order_params.get('amount', 0))
                pos_amount = float(main_pos['contracts'])
                final_amount = req_amount if (0 < req_amount <= pos_amount) else pos_amount
                final_amount = float(self.exchange.amount_to_precision(symbol, final_amount))

                # === A. 限价止盈 (Limit Reduce-Only) - 修改为标准 LIMIT 单 ===
                if action == 'ADD_TP':
                    print(f"💰 [REAL] 设置止盈 (Limit Reduce-Only): {order_side} {final_amount} @ {trigger_price}")
                    params = {
                        'positionSide': position_side, 
                        'timeInForce': 'GTC',
                        'reduceOnly': True # 关键：标记为只减仓，这样它就是一个标准的平仓限价单
                    }
                    # 这里使用 LIMIT 订单类型，而不是 TAKE_PROFIT
                    return self.exchange.create_order(symbol, 'LIMIT', order_side, final_amount, trigger_price, params=params)

                # === B. 止损 (Stop Market) - 保持条件单，但在 open_orders 可见 ===
                # 注意：止损单必须是触发单，因为不能挂比现价差的限价单（会立刻成交）
                elif action == 'ADD_SL':
                    print(f"🛡️ [REAL] 设置止损 (StopMarket): {order_side} {final_amount} @ {trigger_price}")
                    params = {
                        'positionSide': position_side,
                        'stopPrice': trigger_price,
                        # 'reduceOnly': True # StopMarket 隐含 reduceOnly 属性，或通过 closePosition 控制
                    }
                    return self.exchange.create_order(symbol, 'STOP_MARKET', order_side, final_amount, params=params)

        except Exception as e:
            print(f"❌ [REAL Execution Error]: {e}")
            return None