import ccxt
import pandas as pd
import numpy as np
import os
from dotenv import load_dotenv
import time
import warnings
import database
from datetime import datetime
from logger import setup_logger  # 引入 logger

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
        """
        计算体积分布 (Volume Profile) - 优化版
        参考 LuxAlgo 逻辑，采用区间重叠法计算分布
        """
        if len(df) < 50: return None
        
        # 1. 截取数据
        subset = df.iloc[-length:].copy().reset_index(drop=True)
        
        # 2. 定义价格区间
        high_val = subset['high'].max()
        low_val = subset['low'].min()
        
        if high_val == low_val: return None
        
        price_step = (high_val - low_val) / rows
        total_volume = np.zeros(rows)
        
        # 3. 核心计算：将每根K线的成交量分配到对应的价格桶(Bin)中
        # 使用 numpy 向量化操作加速或者是优化后的循环
        # 这里使用优化循环，比 itertuples 快
        
        highs = subset['high'].values
        lows = subset['low'].values
        vols = subset['volume'].values
        
        for i in range(len(subset)):
            h = highs[i]
            l = lows[i]
            v = vols[i]
            
            # 如果是十字星(High=Low)，直接归入对应的一个桶
            if h == l:
                bin_idx = int((h - low_val) / price_step)
                bin_idx = min(bin_idx, rows - 1)
                total_volume[bin_idx] += v
                continue
            
            # 计算该K线覆盖的桶范围
            start_bin = int((l - low_val) / price_step)
            end_bin = int((h - low_val) / price_step)
            
            # 限制范围防止越界
            start_bin = max(0, min(start_bin, rows - 1))
            end_bin = max(0, min(end_bin, rows - 1))
            
            # 计算每单位价格的成交量 (假设均匀分布)
            vol_per_price = v / (h - l)
            
            for b in range(start_bin, end_bin + 1):
                # 当前桶的价格范围
                bin_low = low_val + b * price_step
                bin_high = low_val + (b + 1) * price_step
                
                # 计算 K线 与 当前桶 的重叠高度
                # 重叠 = min(K线顶, 桶顶) - max(K线底, 桶底)
                overlap = max(0, min(h, bin_high) - max(l, bin_low))
                
                # 累加成交量
                total_volume[b] += overlap * vol_per_price

        # 4. 计算 POC (Point of Control)
        poc_idx = np.argmax(total_volume)
        poc_price = low_val + (poc_idx + 0.5) * price_step
        
        # 5. 计算 VAH / VAL (Value Area High / Low)
        total_traded_vol = np.sum(total_volume)
        target_vol = total_traded_vol * va_perc
        
        current_vol = total_volume[poc_idx]
        vah_idx = poc_idx
        val_idx = poc_idx
        
        # 从 POC 向两边扩散寻找 70% 成交量区域
        while current_vol < target_vol:
            # 如果已经到达边界，停止
            if vah_idx >= rows - 1 and val_idx <= 0:
                break
                
            up_vol = total_volume[vah_idx + 1] if vah_idx < rows - 1 else 0
            down_vol = total_volume[val_idx - 1] if val_idx > 0 else 0
            
            if up_vol >= down_vol:
                vah_idx += 1
                current_vol += up_vol
            else:
                val_idx -= 1
                current_vol += down_vol
                
        vah_price = low_val + (vah_idx + 1) * price_step
        val_price = low_val + val_idx * price_step
        
        # 6. 计算 HVN (High Volume Nodes - 筹码峰)
        # 寻找局部峰值 (Local Maxima)
        hvns = []
        # 定义一个简单的窗口来检测峰值，避免噪音
        window = max(1, int(rows * 0.05)) 
        
        for i in range(window, rows - window):
            is_peak = True
            current_val = total_volume[i]
            
            # 检查左右两侧是否都小于当前值
            # 左侧
            if not all(current_val >= total_volume[i-window:i]): is_peak = False
            # 右侧
            if not all(current_val >= total_volume[i+1:i+1+window]): is_peak = False
            
            # 过滤掉太小的峰 (例如小于最大量的 10%)
            if is_peak and current_val > np.max(total_volume) * 0.1:
                hvns.append(low_val + (i + 0.5) * price_step)
        
        # 如果找不到局部峰值，把 POC 放进去作为唯一的峰
        if not hvns:
            hvns.append(poc_price)

        return {
            "poc": poc_price, 
            "vah": vah_price, 
            "val": val_price,
            "hvns": sorted(hvns, reverse=True) # 从高价到低价排序
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
            logger.error(f"Derivatives Error: {e}")
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
                    logger.warning(f"⚠️ [API Warning] 获取订单失败: {e}")
                    status_data["real_open_orders"] = []
                    

            except Exception as e:
                logger.warning(f"⚠️ [Exchange API Warning] 获取实盘数据失败: {e}")
                if status_data["balance"] == 0: status_data["balance"] = 10000 
        else:
            try:
                mock_orders = database.get_mock_orders(symbol)
                status_data["mock_open_orders"] = mock_orders
                status_data["balance"] = 10000.0 
                status_data["real_positions"] = [] 
            except Exception as e:
                logger.error(f"❌ [模拟 DB 错误] 读取数据库失败: {e}")
        return status_data

    def get_market_analysis(self, symbol, mode='STRATEGY'):
        """
        根据模式动态选择 K 线周期
        :param mode: 'REAL' 或 'STRATEGY'
        """
        # ==========================================
        # 1. 动态周期配置
        # ==========================================
        if mode == 'REAL':
            # 实盘：关注短线微观结构，放弃日线以节省资源
            timeframes = ['5m', '15m', '1h', '4h']
        else:
            # 策略：关注宏观长线结构，增加周线(1w)，去除5m噪音
            timeframes = ['15m', '1h', '4h', '1d', '1w']

        final_output = {
            "symbol": symbol,
            "timestamp": int(time.time()),
            "analysis": {},
            "sentiment": self._fetch_market_derivatives(symbol)
        }
        
        logger.info(f"Fetching {symbol} market data ({mode} mode: {timeframes})...")
        
        # 并行获取或顺序获取（这里保持原逻辑顺序获取）
        for tf in timeframes:
            data = self.process_timeframe(symbol, tf)
            if data:
                final_output["analysis"][tf] = data
        
        logger.info("Done.")
        return final_output

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
            
            # 使用新的 VP 算法
            vp = self._calculate_vp(df, length=360)
            if not vp: vp = {"poc": 0, "vah": 0, "val": 0, "hvns": []}
            
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
            logger.error(f"Process TF Error {tf}: {e}")
            return None

    # ==========================================
    # 实盘下单逻辑 (修改：Close 使用 Limit 单)
    # ==========================================
    def place_real_order(self, symbol, action, order_params):
        try:
            if not self.exchange.markets: self.exchange.load_markets()
            symbol = str(symbol)
            
            # --- 1. 撤单逻辑 ---
            if action == 'CANCEL':
                cancel_id = order_params.get('cancel_order_id')
                logger.info(f"🔄 [REAL] 收到撤单指令: ID {cancel_id}")
                try:
                    if cancel_id and cancel_id != "ALL":
                        self.exchange.cancel_order(cancel_id, symbol)
                        logger.info(f"   |-- ✅ 主订单 {cancel_id} 已撤销")
                    return {"status": "cancelled"}
                except Exception as e:
                    logger.error(f"❌ [REAL ERROR] 撤单失败: {e}")
                    return None

            # --- 2. 平仓逻辑 (修改：支持部分平仓/减仓) ---
            if action == 'CLOSE':
                logger.info(f"⚠️ [REAL] 执行 LIMIT 平仓逻辑...")
                try:
                    # 先撤销所有挂单，防止平仓后又成交 (可选，视策略需求，这里保留)
                    # self.exchange.cancel_all_orders(symbol)
                    
                    # 获取 Agent 指定的平仓价格和数量
                    raw_limit_price = float(order_params.get('entry_price', 0))
                    raw_close_amount = float(order_params.get('amount', 0))
                    target_pos_side = order_params.get('pos_side', '').upper()
                    # 如果 Agent 没给价格(或给0)，为了防止报错，我们获取当前最新价作为 Limit 价格
                    if raw_limit_price <= 0:
                        logger.info("   |-- ⚠️ Agent 未指定平仓价，自动获取当前 Ticker 价格...")
                        ticker = self.exchange.fetch_ticker(symbol)
                        raw_limit_price = float(ticker.get('last', 0))

                    positions = self.exchange.fetch_positions([symbol])
                    for pos in positions:
                        total_pos_amt = float(pos['contracts']) # 当前总持仓量
                        
                        if total_pos_amt > 0:

                            side = pos['side'] # long / short
                            current_pos_side_str = 'LONG' if side == 'long' else 'SHORT'

                            if target_pos_side and target_pos_side in ['LONG', 'SHORT']:
                                if target_pos_side != current_pos_side_str:
                                    continue
                            # 决定本次平仓数量
                            # 如果 Agent 指定了数量且小于总持仓，则部分平仓；否则全平
                            if raw_close_amount > 0 and raw_close_amount < total_pos_amt:
                                final_amount = raw_close_amount
                                logger.info(f"   |-- 📉 [部分平仓] 目标: {final_amount} / 持仓: {total_pos_amt}")
                            else:
                                final_amount = total_pos_amt
                                logger.info(f"   |-- 📉 [全仓止盈] 目标: All ({total_pos_amt})")

                            side = pos['side'] # long / short
                            # 平多 = 卖出(Sell) | 平空 = 买入(Buy)
                            close_side = 'sell' if side == 'long' else 'buy'
                            
                            # 格式化价格和数量
                            limit_price = float(self.exchange.price_to_precision(symbol, raw_limit_price))
                            amount = float(self.exchange.amount_to_precision(symbol, final_amount))
                            
                            params = {
                                'positionSide': 'LONG' if side == 'long' else 'SHORT',
                                'timeInForce': 'GTC' # 挂单直到成交
                            }
                            
                            logger.info(f"   |-- 🚀 挂出平仓单: {side} -> {close_side} {amount} @ {limit_price}")
                            self.exchange.create_order(symbol, 'LIMIT', close_side, amount, limit_price, params=params)
                            
                    return {"status": "closing_limit_placed"}
                except Exception as e:
                    logger.error(f"❌ 平仓挂单失败: {e}")
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

                params = {
                    'timeInForce': 'GTC',
                    'positionSide': pos_side, 
                }

                logger.info(f"🚀 [REAL] 发送主限价单: {symbol} {side} {amount} @ {price}")
                
                try:
                    # 1. 下主限价单
                    main_order = self.exchange.create_order(symbol, 'LIMIT', side, amount, price, params=params)
                    logger.info(f"✅ 主订单成功! ID: {main_order['id']}")
                    
                    # 实盘模式通常不自动挂 TP/SL，因为 Agent 会控制 CLOSE
                    logger.info(f"ℹ️ [REAL] 纯限价单模式 (无自动 TP/SL)")
                        
                    return main_order
                except Exception as e:
                    logger.error(f"❌ [REAL API ERROR] 下单失败: {e}")
                    return None

        except Exception as e:
            logger.error(f"❌ [REAL SYSTEM ERROR] 实盘执行异常: {e}")
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
                logger.info(f"   |-- 🛡️ 市价止损已挂: {stop_price}")
            except Exception as e:
                self._handle_order_error(e, "止损")

        # 市价止盈
        if tp_val > 0:
            try:
                tp_price = float(self.exchange.price_to_precision(symbol, tp_val))
                tp_params = base_params.copy()
                tp_params['stopPrice'] = tp_price
                self.exchange.create_order(symbol, 'TAKE_PROFIT_MARKET', close_side, amount, None, params=tp_params)
                logger.info(f"   |-- 💰 市价止盈已挂: {tp_price}")
            except Exception as e:
                self._handle_order_error(e, "止盈")

    def _handle_order_error(self, e, order_type):
        msg = str(e)
        if '2021' in msg: 
            logger.warning(f"   |-- ⚠️ {order_type} 失败: 触发价过于接近现价。")
        elif '2011' in msg:
            logger.warning(f"   |-- ⚠️ {order_type} 暂时拒绝: 仓位未更新。")
        elif '-1106' in msg:
            logger.error(f"   |-- ❌ {order_type} 参数错误: 请检查 reduceOnly。")
        else:
            logger.error(f"   |-- ❌ {order_type} 设置失败: {e}")