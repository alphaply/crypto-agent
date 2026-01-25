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
            # 1. 获取资金费率 (使用专门的 API)
            funding_rate = 0
            try:
                # 币安接口返回的通常是当前生效的费率
                fr_data = self.exchange.fetch_funding_rate(symbol)
                funding_rate = float(fr_data.get('fundingRate', 0))
            except Exception as e:
                # 备选方案：如果 fetch_funding_rate 不支持，尝试从 ticker 的 info 提取
                ticker = self.exchange.fetch_ticker(symbol)
                funding_rate = float(ticker.get('info', {}).get('lastFundingRate', 0))

            # 2. 获取持仓量 (Open Interest)
            try:
                oi_data = self.exchange.fetch_open_interest(symbol)
                oi = float(oi_data.get('openInterestAmount', 0))
            except:
                oi = 0
                
            # 3. 获取 24h 成交额
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
    # 1. 核心数据获取
    # ==========================================

# 在 MarketTool 类中修改这个方法
    def get_account_status(self, symbol, is_real=False):
        """
        根据模式获取账户状态
        :param symbol: 交易对
        :param is_real: True=实盘(请求API), False=模拟(请求数据库)
        """
        # 初始化基础结构
        status_data = {
            "balance": 0,
            "real_positions": [],     # 实盘持仓
            "real_open_orders": [],   # 实盘挂单
            "mock_open_orders": [],   # 模拟挂单
        }

        # ==========================================
        # 🔴 实盘模式：只走交易所网络请求
        # ==========================================
        if is_real:
            try:
                # 1. 获取余额
                balance_info = self.exchange.fetch_balance()
                usdt_balance = float(balance_info.get('USDT', {}).get('free', 0))
                status_data["balance"] = usdt_balance

                # 2. 获取持仓
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

                # 3. 获取挂单
                open_orders_raw = self.exchange.fetch_open_orders(symbol)
                real_open_orders = []
                for o in open_orders_raw:
                    o_type = o.get('type')
                    trigger_price = o.get('stopPrice')
                    if trigger_price is None and 'stopPrice' in o['info']:
                         trigger_price = float(o['info']['stopPrice'])

                    # 格式化显示类型
                    display_type = o_type
                    if o_type == 'STOP_MARKET': display_type = "止损单 (SL)"
                    elif o_type == 'TAKE_PROFIT_MARKET': display_type = "止盈单 (TP)"
                    elif o_type == 'LIMIT': display_type = "限价入场"

                    real_open_orders.append({
                        'order_id': o['id'],
                        'side': o.get('side'),
                        'type': display_type,
                        'raw_type': o_type,
                        'price': o.get('price'),
                        'trigger_price': trigger_price,
                        'amount': o['amount'],
                        'status': o['status'],
                        'datetime': o['datetime']
                    })
                status_data["real_open_orders"] = real_open_orders
                
            except Exception as e:
                print(f"❌ [实盘 API 错误] 获取交易所数据失败: {e}")
                # 实盘失败就是失败，返回空数据，不混杂模拟数据

        # ==========================================
        # 🔵 模拟模式：只走本地数据库
        # ==========================================
        else:
            try:
                # 1. 从数据库获取模拟挂单
                mock_orders = database.get_mock_orders(symbol)
                status_data["mock_open_orders"] = mock_orders
                
                # 2. 模拟余额 (写死一个数，或者你可以做一个数据库表来存模拟余额)
                status_data["balance"] = 10000.0 
                
                # 3. 模拟持仓 
                # 注意：目前你的 database.py 只有 mock_orders 表，没有 mock_positions 表
                # 所以模拟模式下，持仓暂时只能为空，除非你升级数据库逻辑
                status_data["real_positions"] = [] 
                
                # print(f"DEBUG: [模拟] 获取到 {len(mock_orders)} 个挂单") 
            except Exception as e:
                print(f"❌ [模拟 DB 错误] 读取数据库失败: {e}")

        return status_data

        # 初始化默认返回结构
        status_data = {
            "balance": 0,
            "real_positions": [],
            "real_open_orders": [],
            "mock_open_orders": [], # 默认为空
        }

        # --- 第一步：获取本地模拟挂单 (这部分不依赖网络，必须成功) ---
        try:
            mock_orders = database.get_mock_orders(symbol)
            status_data["mock_open_orders"] = mock_orders
            # print(f"DEBUG: Mock Orders found: {len(mock_orders)}") # 调试用
        except Exception as e:
            print(f"❌ [DB Error] 获取模拟挂单失败: {e}")

        # --- 第二步：获取交易所实盘数据 (这部分可能因为网络失败) ---
        try:
            # 1. 获取余额
            balance_info = self.exchange.fetch_balance()
            usdt_balance = float(balance_info.get('USDT', {}).get('free', 0))
            status_data["balance"] = usdt_balance

            # 2. 获取持仓
            all_positions = self.exchange.fetch_positions([symbol])
            real_positions = [
                {
                    'symbol': p['symbol'],
                    'side': p['side'], # LONG / SHORT
                    'amount': float(p['contracts']),
                    'entry_price': float(p['entryPrice']),
                    'unrealized_pnl': float(p['unrealizedPnl'])
                } for p in all_positions if float(p['contracts']) > 0
            ]
            status_data["real_positions"] = real_positions

            # 3. 获取挂单
            open_orders_raw = self.exchange.fetch_open_orders(symbol)
            real_open_orders = []
            
            for o in open_orders_raw:
                # CCXT 标准化字段
                o_type = o.get('type') 
                o_side = o.get('side')
                
                trigger_price = o.get('stopPrice')
                if trigger_price is None and 'stopPrice' in o['info']:
                     trigger_price = float(o['info']['stopPrice'])

                price = o.get('price')

                display_type = o_type
                if o_type == 'STOP_MARKET': display_type = "止损单 (SL)"
                elif o_type == 'TAKE_PROFIT_MARKET': display_type = "止盈单 (TP)"
                elif o_type == 'LIMIT': display_type = "限价入场"

                real_open_orders.append({
                    'order_id': o['id'],
                    'side': o_side,
                    'type': display_type,
                    'raw_type': o_type,
                    'price': price,
                    'trigger_price': trigger_price,
                    'amount': o['amount'],
                    'reduce_only': o['info'].get('reduceOnly', False),
                    'status': o['status'],
                    'datetime': o['datetime']
                })
            
            status_data["real_open_orders"] = real_open_orders

        except Exception as e:
            # 如果是 API 报错，我们只打印警告，但不要让整个函数崩掉
            # 这样模拟盘至少还能拿到 balance=0 和 mock_orders
            print(f"⚠️ [Exchange API Warning] 获取实盘数据失败 (不影响模拟盘运行): {e}")
            # 如果是模拟模式，给个默认余额防止 Agent 报错
            if status_data["balance"] == 0:
                status_data["balance"] = 10000 

        return status_data
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
            if not vp:
                vp = {"poc": 0, "vah": 0, "val": 0, "hvns": [], "lvns": []}

            return {
                "price": close.iloc[-1],
                "rsi": round(rsi, 2),
                "atr": round(atr, 2),
                "ema": {
                    "ema_20": round(ema20, 2),
                    "ema_50": round(ema50, 2),
                    "ema_100": round(ema100, 2),
                    "ema_200": round(ema200, 2)
                },
                "volume_analysis": {
                    "current": round(current_vol, 2),
                    "ma_20": round(vol_ma20, 2),
                    "ratio": vol_ratio,
                    "status": "High" if vol_ratio > 1.2 else "Low"
                },
                "vp": vp,
                "df_raw": df 
            }
        except Exception as e:
            print(f"Process TF Error {tf}: {e}")
            return None

    def get_market_analysis(self, symbol):
        timeframes = ['15m', '1h', '4h', '1d']
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


    def place_real_order(self, symbol, action, order_params):
        """
        实盘下单核心逻辑 (包含智能撤单 & 防僵尸单机制)
        """
        try:
            # 确保连接
            if not self.exchange.markets:
                self.exchange.load_markets()
            
            symbol = str(symbol)
            
            # =======================================================
            # 🛑 1. 智能撤单逻辑 (Smart Cancel) - 解决痛点！
            # =======================================================
            if action == 'CANCEL':
                cancel_id = order_params.get('cancel_order_id')
                print(f"🔄 [REAL] 收到撤单指令: ID {cancel_id}")
                
                try:
                    # 第一步：先尝试撤销指定的主单
                    if cancel_id and cancel_id != "ALL":
                        try:
                            self.exchange.cancel_order(cancel_id, symbol)
                            print(f"   |-- ✅ 主订单 {cancel_id} 已撤销")
                        except Exception as e:
                            # 即使主单撤销失败（比如已经成交或不存在），也要继续检查是否需要清理僵尸单
                            print(f"   |-- ⚠️ 主订单撤销异常 (可能已成交或已撤): {e}")

                    # 第二步：斩草除根逻辑
                    # 查询当前是否还有持仓
                    positions = self.exchange.fetch_positions([symbol])
                    has_position = False
                    for pos in positions:
                        if float(pos['contracts']) > 0:
                            has_position = True
                            print(f"   |-- ⚠️ 检测到当前仍有持仓 ({pos['side']} {pos['contracts']})，保留其余挂单。")
                            break
                    
                    # 第三步：如果没有持仓，为了安全，撤销该币种所有挂单！
                    # 这就是解决“止盈止损还在”的终极办法
                    if not has_position:
                        print(f"   |-- 🛡️ [安全卫士] 检测到无持仓，正在清理所有残留的止盈止损单...")
                        try:
                            self.exchange.cancel_all_orders(symbol)
                            print(f"   |-- ✅✅ {symbol} 所有挂单已清空 (僵尸单已清除)")
                        except Exception as e:
                            print(f"   |-- ❌ 清理僵尸单失败: {e}")
                            
                    return {"status": "cancelled", "clean_sweep": not has_position}

                except Exception as e:
                    print(f"❌ [REAL ERROR] 撤单流程出错: {e}")
                    return None

            # =======================================================
            # 2. 平仓逻辑 (保持不变)
            # =======================================================
            if action == 'CLOSE':
                print(f"⚠️ [REAL] 执行平仓逻辑: 撤单 + 市价平仓")
                try:
                    self.exchange.cancel_all_orders(symbol)
                    positions = self.exchange.fetch_positions([symbol])
                    for pos in positions:
                        amt = float(pos['contracts'])
                        if amt > 0:
                            side = pos['side'] 
                            close_side = 'sell' if side == 'long' else 'buy'
                            params = {'positionSide': 'LONG' if side == 'long' else 'SHORT'}
                            self.exchange.create_order(symbol, 'MARKET', close_side, amt, params=params)
                    return {"status": "closed"}
                except Exception as e:
                    print(f"❌ 平仓失败: {e}")
                    return None

            # =======================================================
            # 3. 开仓挂单逻辑 (建议配合"成交后挂止损"使用)
            # =======================================================
            if action in ['BUY_LIMIT', 'SELL_LIMIT']:
                side = 'buy' if 'BUY' in action else 'sell'
                pos_side = 'LONG' if side == 'buy' else 'SHORT'
                
                raw_amount = float(order_params['amount'])
                raw_price = float(order_params['entry_price'])
                
                amount = float(self.exchange.amount_to_precision(symbol, raw_amount))
                price = float(self.exchange.price_to_precision(symbol, raw_price))

                params = {
                    'timeInForce': 'GTC',
                    'positionSide': pos_side, 
                }

                print(f"🚀 [REAL] 发送主限价单: {symbol} {side} {amount} @ {price}")
                
                try:
                    main_order = self.exchange.create_order(symbol, 'LIMIT', side, amount, price, params=params)
                    print(f"✅ 主订单成功! ID: {main_order['id']}")
                    
                    # 检查是否立即成交
                    if main_order['status'] == 'FILLED':
                        print(f"⚡ 订单已成交，立即挂载止盈止损...")
                        sl_val = float(order_params.get('stop_loss', 0))
                        tp_val = float(order_params.get('take_profit', 0))
                        self._place_sl_tp(symbol, side, pos_side, amount, sl_val, tp_val)
                    else:
                        print(f"⏳ 订单挂单中。注意：如果稍后你撤销此单，系统会自动清理未触发的止盈止损。")
                        
                    return main_order
                except Exception as e:
                    print(f"❌ [REAL API ERROR] 下单失败: {e}")
                    return None

        except Exception as e:
            print(f"❌ [REAL SYSTEM ERROR] 实盘执行异常: {e}")
            return None

    def _place_sl_tp(self, symbol, side, pos_side, amount, sl_val, tp_val):
        """辅助函数：发送止盈止损单"""
        close_side = 'sell' if side == 'buy' else 'buy'
        if sl_val > 0:
            try:
                sl_params = {'positionSide': pos_side, 'stopPrice': sl_val, 'closePosition': True}
                self.exchange.create_order(symbol, 'STOP_MARKET', close_side, amount, params=sl_params)
            except Exception as e: print(f"❌ 止损设置失败: {e}")
        if tp_val > 0:
            try:
                tp_params = {'positionSide': pos_side, 'stopPrice': tp_val, 'closePosition': True}
                self.exchange.create_order(symbol, 'TAKE_PROFIT_MARKET', close_side, amount, params=tp_params)
            except Exception as e: print(f"❌ 止盈设置失败: {e}")