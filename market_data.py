import ccxt
import pandas as pd
import numpy as np
import os
from dotenv import load_dotenv
import time
import warnings
import database
from datetime import datetime

# 忽略 pandas 的一些警告
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
            print("✅ 交易所连接成功，时间已校准。")
        except Exception as e:
            print(f"⚠️ 初始化加载市场失败: {e}")

    # ==========================================
    # 0. 基础工具 (指标计算)
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
            funding_rate = 0
            try:
                fr_data = self.exchange.fetch_funding_rate(symbol)
                funding_rate = float(fr_data.get('fundingRate', 0))
            except Exception:
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
            print(f"Derivatives Error: {e}")
            return {"funding_rate": 0, "open_interest": 0, "24h_quote_vol": 0}

    # ==========================================
    # 1. 获取数据逻辑
    # ==========================================

    def get_account_status(self, symbol, is_real=False):
        status_data = {
            "balance": 0,
            "real_positions": [],
            "real_open_orders": [],
            "mock_open_orders": [],
        }
        if is_real:
            try:
                # 1. 余额
                balance_info = self.exchange.fetch_balance()
                usdt_balance = float(balance_info.get('USDT', {}).get('free', 0))
                status_data["balance"] = usdt_balance

                # 2. 持仓
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

                try:
                    # fetch_open_orders 在 Binance 合约会自动返回：限价单、止损单、止盈单、追踪止损等
                    # 只要是 "Open" (未完全成交且未取消) 的单子都会在里面
                    all_orders = self.exchange.fetch_open_orders(symbol)
                    
                    real_open_orders = []
                    for o in all_orders:
                        # CCXT 的 order 对象里有一个 'info' 字段，里面装着交易所原始返回的完整 JSON
                        # 我们主要依赖 CCXT 解析好的字段，但特殊字段(如 reduceOnly)需要从 info 里取
                        raw = o.get('info', {})

                        # 1. 基础信息
                        o_id = str(o.get('id'))
                        o_side = o.get('side', '').lower()
                        
                        # 2. 类型判断 (优先读取 raw_type 以区分市价止损和限价止损)
                        raw_type = raw.get('type', o.get('type'))
                        
                        # 优化显示逻辑
                        display_type = raw_type
                        if raw_type == 'STOP_MARKET': display_type = "市价止损 (SL-M)"
                        elif raw_type == 'STOP': display_type = "限价止损 (SL-L)"
                        elif raw_type == 'TAKE_PROFIT_MARKET': display_type = "市价止盈 (TP-M)"
                        elif raw_type == 'TAKE_PROFIT': display_type = "限价止盈 (TP-L)"
                        elif raw_type == 'LIMIT': display_type = "限价入场"
                        elif raw_type == 'TRAILING_STOP_MARKET': display_type = "追踪止损"

                        # 3. 价格与触发价
                        # limit price (挂单价)，如果是市价单则是 0
                        price = float(o.get('price') or 0)
                        
                        # trigger price (触发价)。CCXT 通常会解析到 'stopPrice'，如果没有则去 raw 里找
                        trigger_price = float(o.get('stopPrice') or raw.get('stopPrice') or raw.get('activatePrice') or 0)
                        
                        amount = float(o.get('amount', 0))
                        
                        # 4. 特殊属性 (reduceOnly 在 raw info 里)
                        reduce_only = bool(raw.get('reduceOnly', False))
                        
                        # 5. 时间 (CCXT 已经转换好了 datetime 字符串)
                        dt_str = o.get('datetime', '')
                        
                        real_open_orders.append({
                            'order_id': o_id,
                            'side': o_side,
                            'type': display_type,
                            'raw_type': raw_type,
                            'price': price,
                            'trigger_price': trigger_price,
                            'amount': amount,
                            'reduce_only': reduce_only,
                            'status': o.get('status'),
                            'datetime': dt_str
                        })
                    
                    status_data["real_open_orders"] = real_open_orders
                    
                except Exception as e:
                    print(f"⚠️ [API Warning] 获取订单失败: {e}")
                    status_data["real_open_orders"] = []
                    

            except Exception as e:
                print(f"⚠️ [Exchange API Warning] 获取实盘数据失败: {e}")
                if status_data["balance"] == 0: status_data["balance"] = 10000 
        else:
            try:
                mock_orders = database.get_mock_orders(symbol)
                status_data["mock_open_orders"] = mock_orders
                status_data["balance"] = 10000.0 
                status_data["real_positions"] = [] 
            except Exception as e:
                print(f"❌ [模拟 DB 错误] 读取数据库失败: {e}")
        return status_data

    def get_market_analysis(self, symbol):
        timeframes = ['5m','15m', '1h', '4h', '1d']
        final_output = {
            "symbol": symbol,
            "timestamp": int(time.time()),
            "analysis": {},
            "sentiment": self._fetch_market_derivatives(symbol)
        }
        print(f"Fetching {symbol} market data...", end=" ", flush=True)
        for tf in timeframes:
            data = self.process_timeframe(symbol, tf)
            if data:
                final_output["analysis"][tf] = data
        print("Done.")     
        return final_output

# ==========================================
    # 修复后的 process_timeframe (清理了缩进和潜在格式问题)
    # ==========================================
    def process_timeframe(self, symbol, tf):
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, tf, limit=1000)
            if not ohlcv: return None
            df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            df['time'] = pd.to_datetime(df['time'], unit='ms')
            close = df['close']
            volume = df['volume']
            ema20 = self._calc_ema(close, 20).iloc[-1]
            ema50 = self._calc_ema(close, 50).iloc[-1]
            ema100 = self._calc_ema(close, 100).iloc[-1]
            ema200 = self._calc_ema(close, 200).iloc[-1]
            rsi = self._calc_rsi(close, 14).iloc[-1]
            atr = self._calc_atr(df, 14).iloc[-1]
            vol_ma20 = volume.rolling(window=20).mean().iloc[-1]
            current_vol = volume.iloc[-1]
            vol_ratio = round(current_vol / vol_ma20, 2) if vol_ma20 > 0 else 0
            vp = self._calculate_vp(df, length=360)
            if not vp: vp = {"poc": 0, "vah": 0, "val": 0, "hvns": [], "lvns": []}
            
            recent_closes = [round(x, 2) for x in df['close'].tail(5).values.tolist()]
            
            return {
                "price": close.iloc[-1],
                "recent_closes": recent_closes,
                "rsi": round(rsi, 2),
                "atr": round(atr, 2),
                "ema": {"ema_20": round(ema20, 2), "ema_50": round(ema50, 2), "ema_100": round(ema100, 2), "ema_200": round(ema200, 2)},
                "volume_analysis": {"current": round(current_vol, 2), "ma_20": round(vol_ma20, 2), "ratio": vol_ratio, "status": "High" if vol_ratio > 1.2 else "Low"},
                "vp": vp,
                "df_raw": df 
            }
        except Exception as e:
            print(f"Process TF Error {tf}: {e}")
            return None

# ==========================================
    # 修复后的实盘下单逻辑 (纯限价单模式，无自动TP/SL)
    # ==========================================
    def place_real_order(self, symbol, action, order_params):
        try:
            if not self.exchange.markets: self.exchange.load_markets()
            symbol = str(symbol)
            
            # --- 1. 撤单逻辑 ---
            if action == 'CANCEL':
                cancel_id = order_params.get('cancel_order_id')
                print(f"🔄 [REAL] 收到撤单指令: ID {cancel_id}")
                try:
                    if cancel_id and cancel_id != "ALL":
                        self.exchange.cancel_order(cancel_id, symbol)
                        print(f"   |-- ✅ 主订单 {cancel_id} 已撤销")
                    
                    # 联动清理 (可选: 如果你希望撤单时也清理所有其他挂单，可以保留这行)
                    # print(f"   |-- 🧹 [联动清理] 正在撤销 {symbol} 所有挂单...")
                    # self.exchange.cancel_all_orders(symbol)
                    return {"status": "cancelled"}
                except Exception as e:
                    print(f"❌ [REAL ERROR] 撤单失败: {e}")
                    return None

            # --- 2. 平仓逻辑 (修复：Hedge Mode 下删除 reduceOnly) ---
            if action == 'CLOSE':
                print(f"⚠️ [REAL] 执行平仓逻辑...")
                try:
                    # 先撤销所有挂单，防止平仓后又成交
                    self.exchange.cancel_all_orders(symbol)
                    
                    positions = self.exchange.fetch_positions([symbol])
                    for pos in positions:
                        amt = float(pos['contracts'])
                        if amt > 0:
                            side = pos['side'] 
                            # 平多 = 卖出(Sell) | 平空 = 买入(Buy)
                            close_side = 'sell' if side == 'long' else 'buy'
                            params = {
                                'positionSide': 'LONG' if side == 'long' else 'SHORT',
                                # 'reduceOnly': True  <-- 双向持仓模式下禁止使用 reduceOnly
                            }
                            self.exchange.create_order(symbol, 'MARKET', close_side, amt, params=params)
                            print(f"   |-- ✅ {side} 仓位已市价平仓")
                    return {"status": "closed"}
                except Exception as e:
                    print(f"❌ 平仓失败: {e}")
                    return None

            # --- 3. 开仓挂单逻辑 (仅限价单) ---
            if action in ['BUY_LIMIT', 'SELL_LIMIT']:
                side = 'buy' if 'BUY' in action else 'sell'
                pos_side = 'LONG' if side == 'buy' else 'SHORT'
                
                raw_amount = float(order_params['amount'])
                raw_price = float(order_params['entry_price'])
                
                # 精度转换
                amount = float(self.exchange.amount_to_precision(symbol, raw_amount))
                price = float(self.exchange.price_to_precision(symbol, raw_price))

                # 获取 TP/SL (实盘模式下 Agent 会传 0)
                sl_val = float(order_params.get('stop_loss', 0))
                tp_val = float(order_params.get('take_profit', 0))

                params = {
                    'timeInForce': 'GTC',
                    'positionSide': pos_side, 
                }

                print(f"🚀 [REAL] 发送主限价单: {symbol} {side} {amount} @ {price}")
                
                try:
                    # 1. 下主限价单
                    main_order = self.exchange.create_order(symbol, 'LIMIT', side, amount, price, params=params)
                    print(f"✅ 主订单成功! ID: {main_order['id']}")
                    
                    # 2. 判断是否需要挂 TP/SL (实盘模式通常不进入此分支)
                    if sl_val > 0 or tp_val > 0:
                        print(f"⚡ [Hybrid] 正在挂载止盈止损...")
                        self._place_sl_tp_market(symbol, side, pos_side, amount, sl_val, tp_val)
                    else:
                        print(f"ℹ️ [REAL] 纯限价单模式 (无自动 TP/SL)")
                        
                    return main_order
                except Exception as e:
                    print(f"❌ [REAL API ERROR] 下单失败: {e}")
                    return None

        except Exception as e:
            print(f"❌ [REAL SYSTEM ERROR] 实盘执行异常: {e}")
            return None

    def _place_sl_tp_market(self, symbol, side, pos_side, amount, sl_val, tp_val):
        """
        [辅助函数] 如果未来需要在实盘中加入自动止盈止损，可直接调用此函数
        """
        close_side = 'sell' if side == 'buy' else 'buy'
        
        base_params = {
            'positionSide': pos_side,
            # 'reduceOnly': True, # Hedge Mode 必须移除 reduceOnly
            'timeInForce': 'GTC'
        }

        # 市价止损
        if sl_val > 0:
            try:
                stop_price = float(self.exchange.price_to_precision(symbol, sl_val))
                sl_params = base_params.copy()
                sl_params['stopPrice'] = stop_price
                self.exchange.create_order(symbol, 'STOP_MARKET', close_side, amount, None, params=sl_params)
                print(f"   |-- 🛡️ 市价止损已挂: {stop_price}")
            except Exception as e:
                self._handle_order_error(e, "止损")

        # 市价止盈
        if tp_val > 0:
            try:
                tp_price = float(self.exchange.price_to_precision(symbol, tp_val))
                tp_params = base_params.copy()
                tp_params['stopPrice'] = tp_price
                self.exchange.create_order(symbol, 'TAKE_PROFIT_MARKET', close_side, amount, None, params=tp_params)
                print(f"   |-- 💰 市价止盈已挂: {tp_price}")
            except Exception as e:
                self._handle_order_error(e, "止盈")

    def _handle_order_error(self, e, order_type):
        msg = str(e)
        if '2021' in msg: 
            print(f"   |-- ⚠️ {order_type} 失败: 触发价过于接近现价。")
        elif '2011' in msg:
            print(f"   |-- ⚠️ {order_type} 暂时拒绝: 仓位未更新。")
        elif '-1106' in msg:
            print(f"   |-- ❌ {order_type} 参数错误: 请检查 reduceOnly。")
        else:
            print(f"   |-- ❌ {order_type} 设置失败: {e}")