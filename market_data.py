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
    # 1. 核心数据获取
    # ==========================================
    def get_account_status(self, symbol):
        try:
            balance_info = self.exchange.fetch_balance()
            usdt_balance = float(balance_info.get('USDT', {}).get('free', 0))

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

            open_orders_raw = self.exchange.fetch_open_orders(symbol)
            real_open_orders = []
            for o in open_orders_raw:
                order_type = o['info'].get('type')
                trigger_price = o.get('stopPrice') or o['info'].get('stopPrice')
                price = o.get('price')

                real_open_orders.append({
                    'order_id': o['id'],
                    'side': o['side'],
                    'type': o['type'],
                    'price': price,
                    'trigger_price': float(trigger_price) if trigger_price else None,
                    'amount': o['amount'],
                    'reduce_only': o['info'].get('reduceOnly', False),
                    'status': o['status'],
                    'datetime': o['datetime']
                })

            mock_orders = database.get_mock_orders(symbol)
            
            return {
                "balance": usdt_balance,
                "real_positions": real_positions,
                "real_open_orders": real_open_orders,
                "mock_open_orders": mock_orders,
            }
        except Exception as e:
            print(f"❌ Account Status Error: {e}")
            return {"balance": 0, "real_positions": [], "real_open_orders": [], "mock_open_orders": []}

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
        实盘下单核心逻辑 (最终修正版：OTO下单 + 连带撤单)
        :param symbol: 交易对 (e.g. 'BTC/USDT:USDT')
        :param action: 动作类型 ('BUY_LIMIT', 'SELL_LIMIT', 'CLOSE', 'CANCEL')
        :param order_params: 字典
        """
        try:
            self.exchange.load_markets()
            symbol = str(symbol)
            
            # 1. 撤单逻辑 (安全模式：强制全撤)
            if action == 'CANCEL':
                print(f"🚫 [REAL] 收到撤单指令，正在清理 {symbol} 所有挂单 (防止条件单残留)...")
                try:
                    return self.exchange.cancel_all_orders(symbol)
                except Exception as e:
                    print(f"⚠️ [REAL] 撤单提示: {e}")
                    return None

            # 2. 平仓逻辑
            if action == 'CLOSE':
                print(f"🧹 [REAL] 平仓前清理 {symbol} 所有挂单...")
                try:
                    self.exchange.cancel_all_orders(symbol)
                except: pass

                positions = self.exchange.fetch_positions([symbol])
                active_positions = [p for p in positions if float(p['contracts']) > 0]
                
                if not active_positions:
                    print(f"ℹ️ [REAL] {symbol} 无需平仓：当前无持仓")
                    return None
                    
                results = []
                for pos in active_positions:
                    pos_side = pos['side']
                    amount = float(pos['contracts'])
                    side = 'sell' if pos_side == 'LONG' else 'buy'
                    
                    print(f"📉 [REAL] 执行市价平仓: {symbol} {pos_side} {amount}")
                    order = self.exchange.create_order(
                        symbol=symbol,
                        type='MARKET',
                        side=side,
                        amount=amount,
                        params={'positionSide': pos_side}
                    )
                    results.append(order)
                return results

            # 3. 开仓挂单逻辑 (OTO 模式：合并止盈止损)
            if action in ['BUY_LIMIT', 'SELL_LIMIT']:
                side = 'buy' if 'BUY' in action else 'sell'
                pos_side = 'LONG' if side == 'buy' else 'SHORT'
                
                # A. 精度处理
                amount_str = self.exchange.amount_to_precision(symbol, order_params['amount'])
                price_str = self.exchange.price_to_precision(symbol, order_params['entry_price'])
                
                amount = float(amount_str)
                price = float(price_str)

                # B. 构建核心参数
                params = {
                    'timeInForce': 'GTC',
                    'positionSide': pos_side,
                }

                # C. 注入止盈止损 (核心！合并发送)
                sl_val = order_params.get('stop_loss', 0)
                tp_val = order_params.get('take_profit', 0)

                if sl_val > 0:
                    params['stopLossPrice'] = self.exchange.price_to_precision(symbol, sl_val)
                
                if tp_val > 0:
                    params['takeProfitPrice'] = self.exchange.price_to_precision(symbol, tp_val)

                print(f"🚀 [REAL] 发送 OTO 组合单: {symbol} {side} {pos_side}")
                print(f"   主单: {amount} @ {price}")
                print(f"   附带止损: {params.get('stopLossPrice', '无')} | 附带止盈: {params.get('takeProfitPrice', '无')}")

                # D. 发送唯一的 Create Order
                main_order = self.exchange.create_order(
                    symbol=symbol,
                    type='LIMIT',
                    side=side,
                    amount=amount,
                    price=price,
                    params=params
                )
                
                print(f"✅ 下单成功! 主单ID: {main_order['id']}")
                return main_order

        except Exception as e:
            print(f"❌ [REAL] 实盘执行异常: {e}")
            return None