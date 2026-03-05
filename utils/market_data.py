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
from utils.indicators import (
    smart_fmt, calc_ema, calc_rsi, calc_stoch_rsi, calc_atr,
    calc_macd, calc_kdj, calc_cci, calc_adx, calc_vwap,
    calc_bollinger_bands, calculate_vp
)
import uuid
import math

logger = setup_logger("MarketData")
warnings.filterwarnings("ignore")
load_dotenv()

class MarketTool:
    def __init__(self, config_id: str = None, symbol: str = None, proxy_port=None):
        """
        初始化交易所连接
        :param config_id: 配置ID（推荐使用，支持多个相同交易对）
        :param symbol: 交易对符号（向后兼容，不推荐）
        :param proxy_port: 本地代理端口 (例如 7890 或 10809), None 为直连
        """
        from config import config as global_config

        # 优先使用 config_id，如果没有则使用 symbol（向后兼容）
        if config_id:
            # 通过 config_id 获取完整配置
            cfg = global_config.get_config_by_id(config_id)
            if not cfg:
                raise ValueError(f"未找到配置ID: {config_id}")
            self.config_id = config_id
            self.symbol = cfg.get('symbol')
            api_key, secret = global_config.get_binance_credentials(config_id=config_id)
            mode = cfg.get('mode', 'STRATEGY').upper()
            market_type = cfg.get('market_type', 'swap')
            if mode == 'SPOT_DCA':
                market_type = 'spot'
        elif symbol:
            # 向后兼容：使用 symbol 查询
            logger.warning(f"⚠️ 使用 symbol 初始化已过时，建议使用 config_id")
            self.config_id = None
            self.symbol = symbol
            api_key, secret = global_config.get_binance_credentials(symbol=symbol)
            cfg = None
            market_type = 'swap'
        else:
            raise ValueError("必须提供 config_id 或 symbol")

        if not api_key or not secret:
            raise ValueError(f"未找到币安API配置 (config_id={config_id}, symbol={symbol})")

        config = {
            'apiKey': api_key,
            'secret': secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': market_type,
                'adjustForTimeDifference': True,
                'recvWindow': global_config.DEFAULT_RECVWINDOW,
            }
        }

        if proxy_port:
            config['proxies'] = {
                'http': f'http://127.0.0.1:{proxy_port}',
                'https': f'http://127.0.0.1:{proxy_port}',
            }

        if market_type == 'spot':
            self.exchange = ccxt.binance(config)
        else:
            self.exchange = ccxt.binanceusdm(config)

        try:
            self.exchange.load_markets()
            logger.info(f"✅ 交易所连接成功 [config_id={config_id}, symbol={self.symbol}]")
        except Exception as e:
            logger.warning(f"⚠️ 初始化加载市场失败 [config_id={config_id}, symbol={self.symbol}]: {e}")

    # ==========================================
    # 0. 基础工具 (衍生数据获取)
    # ==========================================

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

    def get_account_status(self, symbol, is_real=False, agent_name=None, config_id=None):
        status_data = {
            "balance": 0,
            "real_positions": [],
            "real_open_orders": [],
            "mock_open_orders": [],
        }
        
        try:
            if is_real:
                balance_info = self.exchange.fetch_balance()
                
                # 兼容性处理：尝试从不同可能的路径获取 USDT 余额
                # ccxt 在 fetch_balance() 中通常会统一化结构，但在某些交易所 API 变动时可能失效
                usdt_free = 0
                if 'USDT' in balance_info:
                    usdt_free = float(balance_info['USDT'].get('free', 0))
                elif 'usdt' in balance_info:
                    usdt_free = float(balance_info['usdt'].get('free', 0))
                elif 'total' in balance_info and 'USDT' in balance_info['total']:
                    # 某些版本或模式下可能在 total 字段中
                    usdt_free = float(balance_info['free'].get('USDT', 0))
                
                status_data["balance"] = usdt_free
                
                # 实盘持仓
                try:
                    if self.exchange.options.get('defaultType') == 'spot':
                        # 现货没有持仓概念，通过查询币种余额代替，为了简化这里暂返回空持仓
                        status_data["real_positions"] = []
                    else:
                        all_positions = self.exchange.fetch_positions([symbol])
                        real_positions = [
                            {
                                'symbol': p['symbol'],
                                'side': str(p.get('side', '')).upper(), # 'LONG' or 'SHORT'
                                'amount': float(p['contracts']),
                                'entry_price': float(p['entryPrice']),
                                'unrealized_pnl': float(p['unrealizedPnl'])
                            } for p in all_positions if float(p['contracts']) > 0
                        ]
                        status_data["real_positions"] = real_positions
                except Exception as e:
                    logger.warning(f"Fetch real positions error (maybe spot market?): {e}")
                    status_data["real_positions"] = []

                # 实盘挂单
                try:
                    # 核心修改：增加 params={'trigger': True} 以拉取条件委托/触发单
                    all_orders = self.exchange.fetch_open_orders(symbol, params={'trigger': True})
                    logger.info(f"[{symbol}] Fetched {len(all_orders)} open orders (including triggers)")
                    for i, o in enumerate(all_orders):
                        logger.info(f"  Order #{i}: ID={o.get('id')} Type={o.get('type')} Status={o.get('status')} Price={o.get('price')} StopPrice={o.get('stopPrice')} InfoStop={o.get('info', {}).get('stopPrice')}")
                    
                    filtered_orders = []
                    for o in all_orders:
                        # 从 exchange 的原始响应中提取 positionSide (针对币安 USDM)
                        # ccxt 统一结构中通常在 o['info']['positionSide']
                        info = o.get('info', {})
                        pos_side = info.get('positionSide', 'BOTH')
                        
                        # 核心修复：处理条件单 (STOP_MARKET, TAKE_PROFIT_MARKET 等)
                        # 这些订单在 fetch_open_orders 中 price 为 0，实际价格在 stopPrice 中
                        order_type = o.get('type', '').upper()
                        price = float(o.get('price') or 0)
                        
                        # 如果是条件单且价格为0，则使用触发价作为显示价格
                        if price == 0 and 'stopPrice' in info:
                            price = float(info['stopPrice'])
                        
                        # 增强类型显示
                        display_type = order_type
                        if 'STOP' in order_type: display_type = "STOP"
                        if 'TAKE_PROFIT' in order_type: display_type = "TP"

                        filtered_orders.append({
                            'order_id': str(o.get('id')),
                            'side': o.get('side', '').lower(),
                            'pos_side': pos_side.upper(), # 'LONG', 'SHORT' or 'BOTH'
                            'type': display_type,
                            'price': price,
                            'amount': float(o.get('amount', 0)),
                            'status': o.get('status')
                        })
                    status_data["real_open_orders"] = filtered_orders
                except Exception as e:
                    logger.warning(f"Fetch real orders error: {e}")
            else:
                # 模拟模式
                status_data["balance"] = 10000.0 
                # 同时传入 config_id 和 agent_name 以获得最佳兼容性
                status_data["mock_open_orders"] = database.get_mock_orders(symbol, agent_name=agent_name, config_id=config_id)
                
        except Exception as e:
            logger.error(f"Account Status Error: {e}")
        
        return status_data

    def get_market_analysis(self, symbol, mode='STRATEGY', timeframes=None):
        """
        全量获取市场数据的主入口
        """
        if timeframes is None:
            timeframes = ['5m', '15m', '1h', '4h', '1d', '1w']

        final_output = {
            "symbol": symbol,
            "timestamp": int(time.time()),
            "analysis": {},
            "sentiment": self._fetch_market_derivatives(symbol)
        }

        logger.info(f"📊 Fetching {symbol} market data ({mode} mode: {timeframes})...")

        for tf in timeframes:
            logger.debug(f"  → Processing timeframe: {tf}")
            data = self.process_timeframe(symbol, tf)
            if data:
                final_output["analysis"][tf] = data
                logger.debug(f"  ✅ {tf} data collected (price: {data.get('price', 'N/A')})")
            else:
                logger.warning(f"  ⚠️ {tf} data is None, skipping")

        logger.info(f"✅ Market analysis complete. Collected {len(final_output['analysis'])} timeframes")
        return final_output

    def process_timeframe(self, symbol, tf):
        """
        处理单个时间周期的核心逻辑（含指标计算升级）
        """
        try:
            logger.debug(f"    🔍 [{tf}] Fetching OHLCV data for {symbol}...")
            # 1. 获取 OHLCV
            # limit 调大到 1000，确保 EMA200 等长周期指标有足够的预热数据
            ohlcv = self.exchange.fetch_ohlcv(symbol, tf, limit=1000)
            if not ohlcv or len(ohlcv) < 200:
                logger.warning(f"    ⚠️ [{tf}] Insufficient OHLCV data for reliable indicators: {len(ohlcv) if ohlcv else 0} candles (need >= 200)")
                return None

            logger.debug(f"    ✅ [{tf}] Got {len(ohlcv)} candles, calculating indicators...")
            df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            df['time'] = pd.to_datetime(df['time'], unit='ms')
            
            # 提取 Series
            close = df['close']
            high = df['high']
            low = df['low']
            volume = df['volume']
            
            # ================= 计算指标 =================
            # 1. 基础均线
            ema20 = calc_ema(close, 20)
            ema50 = calc_ema(close, 50)
            ema100 = calc_ema(close, 100)
            ema200 = calc_ema(close, 200)
            
            # 2. 动量与震荡
            rsi = calc_rsi(close, 14)
            stoch_k, stoch_d = calc_stoch_rsi(rsi)
            atr = calc_atr(df, 14)
            macd, signal, hist = calc_macd(close)
            k, d, j = calc_kdj(df)
            cci = calc_cci(df)
            
            # 3. 趋势强度 (ADX) 与 价值中枢 (VWAP)
            adx, plus_di, minus_di = calc_adx(df)
            vwap = calc_vwap(df)
            
            # 4. 布林带 (判断波动率挤压)
            bb_up, bb_mid, bb_low, bb_width = calc_bollinger_bands(close)
            
            # 5. 成交量分析
            vol_ma20 = volume.rolling(window=20).mean()
            vol_ratio = (volume / vol_ma20).fillna(0)
            
            # 6. VP 分布
            vp = calculate_vp(df, length=360)
            if not vp: vp = {"poc": 0, "vah": 0, "val": 0, "hvns": []}
            
            # ================= 提取最新值 =================
            curr_close = close.iloc[-1]
            
            # 趋势判定逻辑
            trend_status = "Consolidation"
            e20_val = ema20.iloc[-1]
            e50_val = ema50.iloc[-1]
            e200_val = ema200.iloc[-1]
            if e20_val > e50_val > e200_val: trend_status = "Strong Uptrend"
            elif e20_val < e50_val < e200_val: trend_status = "Strong Downtrend"
            elif curr_close > e200_val: trend_status = "Bullish Neutral"
            elif curr_close < e200_val: trend_status = "Bearish Neutral"

            # 趋势强度 (ADX > 25 表示强趋势)
            adx_val = float(adx.iloc[-1])
            trend_strength = "Strong" if adx_val > 25 else "Weak/Ranging"
            
            # ================= 序列数据提取 =================
            def to_list(series, n=5):
                raw = series.iloc[-n:].values.tolist()
                return [smart_fmt(float(x)) for x in raw]

            recent_closes = to_list(close)
            recent_highs = to_list(high)
            recent_lows = to_list(low)

            result = {
                "price": smart_fmt(curr_close),
                "trend": {
                    "status": trend_status,
                    "strength": trend_strength,
                    "adx": round(adx_val, 1)
                },
                "vwap": smart_fmt(vwap.iloc[-1]),
                "recent_closes": recent_closes,
                "recent_highs": recent_highs,
                "recent_lows": recent_lows,

                "rsi_analysis": {
                    "rsi": round(float(rsi.iloc[-1]), 1),
                    "stoch_k": round(float(stoch_k.iloc[-1]), 1),
                    "stoch_d": round(float(stoch_d.iloc[-1]), 1),
                },
                "cci": round(float(cci.iloc[-1]), 1),
                "kdj": {
                    "k": round(float(k.iloc[-1]), 1),
                    "d": round(float(d.iloc[-1]), 1),
                    "j": round(float(j.iloc[-1]), 1)
                },

                "atr": smart_fmt(atr.iloc[-1]),
                "macd": {
                    "diff": smart_fmt(macd.iloc[-1]),
                    "dea": smart_fmt(signal.iloc[-1]),
                    "hist": smart_fmt(hist.iloc[-1])
                },
                "bollinger": {
                    "up": smart_fmt(bb_up.iloc[-1]),
                    "mid": smart_fmt(bb_mid.iloc[-1]),
                    "low": smart_fmt(bb_low.iloc[-1]),
                    "width": round(float(bb_width.iloc[-1]), 4)
                },

                "ema": {
                    "ema_20": smart_fmt(e20_val),
                    "ema_50": smart_fmt(e50_val),
                    "ema_100": smart_fmt(ema100.iloc[-1]),
                    "ema_200": smart_fmt(e200_val)
                },

                "volume_analysis": {
                    "current": smart_fmt(volume.iloc[-1]),
                    "ratio": round(float(vol_ratio.iloc[-1]), 2),
                    "status": "High" if float(vol_ratio.iloc[-1]) > 1.5 else ("Low" if float(vol_ratio.iloc[-1]) < 0.5 else "Normal")
                },

                "vp": vp
            }

            logger.debug(f"    ✅ [{tf}] Indicators calculated successfully (price={result['price']}, atr={result['atr']})")
            return result
        except Exception as e:
            logger.error(f"❌ Process TF Error [{tf}]: {e}")
            import traceback
            logger.error(f"Traceback:\n{traceback.format_exc()}")
            return None

    # ==========================================
    # 实盘下单逻辑
    # ==========================================
    def place_real_order(self, symbol, action, order_params, agent_name=None):
        try:
            if not self.exchange.markets: self.exchange.load_markets()
            symbol = str(symbol)
            
            # --- 日志：收到指令 ---
            logger.info(f"🔔 [REAL_ORDER] 收到指令: {symbol} | {action} | Params: {order_params}")

            if action == 'CANCEL':
                cancel_id = order_params.get('cancel_order_id')
                if cancel_id:
                    logger.info(f"🔄 [CANCEL] 正在撤单 ID: {cancel_id} ...")
                    try:
                        res = self.exchange.cancel_order(cancel_id, symbol)
                        logger.info(f"✅ [CANCEL] 撤单成功: {cancel_id}")
                        return {"status": "cancelled", "response": res}
                    except Exception as e:
                        logger.error(f"❌ [CANCEL] 撤单失败: {e}")
                return None

            if action == 'CLOSE':
                raw_close_amount = float(order_params.get('amount', 0))
                raw_close_price = float(order_params.get('entry_price', 0))
                target_pos_side = order_params.get('pos_side', '').upper()
                logger.info(f"🔍 [CLOSE] 检查持仓... 目标: {target_pos_side} | 量: {raw_close_amount} | 价: {raw_close_price}")
                positions = self.exchange.fetch_positions([symbol])
                orders_executed = []

                for pos in positions:
                    amt = float(pos['contracts']) # 当前持仓数量
                    side = pos['side']            # 'long' or 'short'
                    ticker = self.exchange.fetch_ticker(symbol)
                    current_price = float(ticker['last'])
                    # 过滤方向：如果指定了只平 SHORT，就跳过 LONG
                    current_pos_side_str = 'LONG' if side == 'long' else 'SHORT'
                    if target_pos_side and target_pos_side != current_pos_side_str:
                        continue

                    if amt > 0:
                        # 确定交易方向：平多=Sell，平空=Buy
                        close_side = 'sell' if side == 'long' else 'buy'
                        
                        # 确定数量：部分平仓 vs 全平
                        final_amt = raw_close_amount if (0 < raw_close_amount < amt) else amt
                        formatted_amt = self.exchange.amount_to_precision(symbol, final_amt)

                        params = {'positionSide': current_pos_side_str}
                        
                        order = None
                        if raw_close_price > 0:
                            formatted_price = self.exchange.price_to_precision(symbol, raw_close_price)
                            is_stop_loss = False

                            if side == 'long' and float(formatted_price) < current_price:
                                is_stop_loss = True
                            # 平空(Buy): 价格高于现价 -> 止损
                            elif side == 'short' and float(formatted_price) > current_price:
                                is_stop_loss = True

                            if is_stop_loss:
                                logger.info(f"🛑 [CLOSE-STOP] 检测到止损场景 (现价 {current_price} -> 目标 {formatted_price})")
                                
                                # 方案 A: 止损市价单 (推荐，保证止损触发后立刻跑路)
                                order_type = 'STOP_MARKET' # STOP / STOP_LIMIT
                                params['stopPrice'] = float(formatted_price) # 触发价格
                                params['closePosition'] = True # 某些交易所支持直接平仓标志
                                
                                # 注意：STOP_MARKET 通常不需要传 price 参数 (传 None)，但需要 stopPrice
                                order = self.exchange.create_order(symbol, order_type, close_side, final_amt, None, params=params)

                            else:
                                logger.info(f"💰 [CLOSE-TP] 检测到止盈场景 (现价 {current_price} -> 目标 {formatted_price})")
                                order_type = 'LIMIT'
                                params['timeInForce'] = 'GTC'
                                order = self.exchange.create_order(symbol, order_type, close_side, final_amt, float(formatted_price), params=params)
                        else:
                            # 2. 市价平仓 (Market Close)
                            order_type = 'MARKET'
                            logger.info(f"🚀 [CLOSE-MARKET] 下单: {current_pos_side_str} -> {close_side} {formatted_amt} @ 市价")
                            order = self.exchange.create_order(symbol, order_type, close_side, final_amt, params=params)
                        
                        if order:
                            orders_executed.append(order)

                if orders_executed:
                    logger.info(f"✅ [CLOSE] 平仓指令执行完毕，共 {len(orders_executed)} 个订单")
                    return orders_executed[0] if len(orders_executed) == 1 else {"status": "closed", "orders": orders_executed}
                else:
                    logger.warning(f"⚠️ [CLOSE] 未找到对应方向的持仓或持仓为0，跳过")
                    return {"status": "no_position"}

            if action in ['BUY_LIMIT', 'SELL_LIMIT']:
                side = 'buy' if 'BUY' in action else 'sell'
                
                amount = self.exchange.amount_to_precision(symbol, float(order_params['amount']))
                price = self.exchange.price_to_precision(symbol, float(order_params['entry_price']))
                
                params = {'timeInForce': 'GTC'}
                
                is_spot = (self.exchange.options.get('defaultType') == 'spot')
                if not is_spot:
                    pos_side = 'LONG' if side == 'buy' else 'SHORT'
                    params['positionSide'] = pos_side
                    logger.info(f"🚀 [OPEN-LIMIT] 开仓挂单: {pos_side} {side} {amount} @ {price}")
                else:
                    logger.info(f"🚀 [SPOT-LIMIT] 现货挂单: {side} {amount} @ {price}")
                
                order = self.exchange.create_order(symbol, 'LIMIT', side, float(amount), float(price), params=params)
                
                logger.info(f"✅ [OPEN-LIMIT] 挂单成功 ID: {order['id']}")
                return order

        except ccxt.InsufficientFunds as e:
            logger.error(f"❌ [ORDER_ERROR] 资金不足: {e}")
            raise e
        except ccxt.NetworkError as e:
            logger.error(f"❌ [ORDER_ERROR] 网络异常: {e}")
            raise e
        except ccxt.ExchangeError as e:
            logger.error(f"❌ [ORDER_ERROR] 交易所API异常: {e}")
            raise e
        except Exception as e:
            logger.error(f"❌ [ORDER_ERROR] 执行异常: {e}")
            raise e
            
    def fetch_recent_trades(self, symbol, limit=20):
        try:
            return self.exchange.fetch_my_trades(symbol, limit=limit)
        except:
            return []
