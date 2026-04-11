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
    smart_fmt, calc_ema, calc_rsi, calc_atr,
    calc_macd, calc_adx, calc_vwap,
    calc_bollinger_bands, calculate_vp,
    detect_rsi_divergence
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
            exchange_name, api_key, secret, passphrase = global_config.get_exchange_credentials(config_id=config_id)
            mode = cfg.get('mode', 'STRATEGY').upper()
            market_type = cfg.get('market_type', 'swap')
            if mode == 'SPOT_DCA':
                market_type = 'spot'
        elif symbol:
            # 向后兼容：使用 symbol 查询
            logger.warning(f"⚠️ 使用 symbol 初始化已过时，建议使用 config_id")
            self.config_id = None
            self.symbol = symbol
            exchange_name, api_key, secret, passphrase = global_config.get_exchange_credentials(symbol=symbol)
            cfg = None
            market_type = 'swap'
        else:
            raise ValueError("必须提供 config_id 或 symbol")

        if not api_key or not secret:
            raise ValueError(f"未找到{exchange_name} API配置 (config_id={config_id}, symbol={symbol})")

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
        
        # OKX 额外需要密码 (Passphrase)
        if exchange_name == 'okx' and passphrase:
            config['password'] = passphrase

        if proxy_port:
            config['proxies'] = {
                'http': f'http://127.0.0.1:{proxy_port}',
                'https': f'http://127.0.0.1:{proxy_port}',
            }

        if exchange_name == 'okx':
            self.exchange = ccxt.okx(config)
        elif market_type == 'spot':
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
        """获取资金费率、持仓量、多空比、爆仓量等衍生品数据"""
        try:
            funding_rate = 0
            try:
                fr_data = self.exchange.fetch_funding_rate(symbol)
                funding_rate = float(fr_data.get('fundingRate', 0))
            except:
                ticker = self.exchange.fetch_ticker(symbol)
                funding_rate = float(ticker.get('info', {}).get('lastFundingRate', 0))

            try:
                oi_data = self.exchange.fetch_open_interest(symbol)
                oi = float(oi_data.get('openInterestAmount', 0))
            except:
                oi = 0
                
            ticker = self.exchange.fetch_ticker(symbol)
            quote_vol = float(ticker.get('quoteVolume', 0))

            # --- 新增：币安特定深度情绪指标 ---
            binance_sentiment = self._fetch_binance_specific_sentiment(symbol)
                
            return {
                "funding_rate": funding_rate,
                "open_interest": oi,
                "24h_quote_vol": quote_vol,
                **binance_sentiment
            }
        except Exception as e:
            logger.error(f"Derivatives Error: {e}")
            return {"funding_rate": 0, "open_interest": 0, "24h_quote_vol": 0}

    def _fetch_binance_specific_sentiment(self, symbol):
        """
        专门获取币安的多空人数比、大户持仓比等（仅限合约）
        """
        sentiment = {
            "ls_ratio": "N/A",
            "ls_accounts": "N/A",
            "liquidations_24h": "N/A"
        }
        
        # 仅限于 Binance USDM 合约
        if self.exchange.id != 'binanceusdm' and self.exchange.id != 'binance':
            return sentiment

        try:
            # 去掉 symbol 中的 :USDT 等后缀，币安 API 通常只需要 BTCUSDT
            clean_symbol = symbol.replace(':', '').replace('/', '')
            
            # 1. 全球多空人数比 (Global Long/Short Account Ratio)
            try:
                ls_data = self.exchange.fapiDataGetGlobalLongShortAccountRatio({'symbol': clean_symbol, 'period': '5m'})
                if ls_data and len(ls_data) > 0:
                    sentiment["ls_accounts"] = float(ls_data[-1]['longShortRatio'])
            except: pass

            # 2. 大户持仓多空比 (Top Trader Long/Short Ratio)
            try:
                ls_positions = self.exchange.fapiDataGetTopLongShortPositionRatio({'symbol': clean_symbol, 'period': '5m'})
                if ls_positions and len(ls_positions) > 0:
                    sentiment["ls_ratio"] = float(ls_positions[-1]['longShortRatio'])
            except: pass

            # 3. 24h 爆仓数据 (通常需要从 WebSocket 或专门爬取，但 CCXT 部分交易所支持 fetch_liquidations)
            # 币安没有直接的 API 获取全量历史爆仓，通常需要订阅。这里暂留或尝试 ticker 中的隐含信息。
            
        except Exception as e:
            logger.debug(f"Binance Specific Sentiment Fetch Failed: {e}")
            
        return sentiment

    # ==========================================
    # 1. 获取数据逻辑
    # ==========================================

    def _check_mock_orders_tp_sl(self, symbol, current_high, current_low, candle_ts_ms=0):
        """检查模拟盘订单是否触及止盈止损并自动平仓"""
        if not self.config_id: return
        mock_orders = database.get_mock_orders(symbol=symbol, config_id=self.config_id)
        if not mock_orders: return
        
        for o in mock_orders:
            try:
                # 确保检查的K线晚于订单创建时间
                if candle_ts_ms > 0:
                    order_time_ms = datetime.strptime(o['timestamp'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=database.TZ_CN).timestamp() * 1000
                    if candle_ts_ms <= order_time_ms:
                        continue

                side = o.get('side', '').upper()
                sl = float(o.get('stop_loss') or 0)
                tp = float(o.get('take_profit') or 0)
                entry = float(o.get('price') or 0)
                amount = float(o.get('amount') or 0)
                is_filled = int(o.get('is_filled') or 0)
                
                # 入场检测
                if not is_filled:
                    if 'BUY' in side and current_low <= entry:
                        is_filled = 1
                        database.update_mock_order_filled(o['order_id'])
                    elif 'SELL' in side and current_high >= entry:
                        is_filled = 1
                        database.update_mock_order_filled(o['order_id'])
                        
                # 只有入场后才检测止盈止损
                if not is_filled:
                    continue
                
                close_price = 0
                reason = ""
                
                if 'BUY' in side:
                    if sl > 0 and current_low <= sl:
                        close_price = sl
                        reason = f"📉 触及止损价 {sl}"
                    elif tp > 0 and current_high >= tp:
                        close_price = tp
                        reason = f"🎯 触及止盈价 {tp}"
                elif 'SELL' in side:
                    if sl > 0 and current_high >= sl:
                        close_price = sl
                        reason = f"📉 触及止损价 {sl}"
                    elif tp > 0 and current_low <= tp:
                        close_price = tp
                        reason = f"🎯 触及止盈价 {tp}"
                        
                if close_price > 0:
                    realized_pnl = (close_price - entry) * amount * (1 if 'BUY' in side else -1)
                    database.close_mock_order(o['order_id'], close_price=close_price, realized_pnl=realized_pnl)
                    logger.info(f"⚡ [Auto TP/SL] {symbol} 模拟单 {o['order_id']} 自动平仓: {reason}, PnL={realized_pnl:.4f}, candle_ts={candle_ts_ms}")
                    database.save_order_log(o['order_id'] + "_AUTO", symbol, o['agent_name'], f"CLOSE_{side}", close_price, tp, sl, f"[智能盯盘] {reason} (CandleTS: {candle_ts_ms})", trade_mode="STRATEGY", config_id=self.config_id, amount=amount)
            except Exception as e:
                logger.error(f"❌ _check_mock_orders_tp_sl error for {o.get('order_id')}: {e}")

    def run_silent_sl_tp(self):
        """[Phase 3] 仅执行盈亏检测，不运行策略。由调度器每分钟触发，实现实时止盈止损。"""
        if not self.config_id or not self.symbol: return
        try:
            # 获取最近 1 分钟的 K线，用其 High/Low 进行检测
            ohlcv = self.exchange.fetch_ohlcv(self.symbol, '1m', limit=1)
            if ohlcv:
                # [timestamp, open, high, low, close, volume]
                candle_ts_ms = int(ohlcv[0][0])
                h = float(ohlcv[0][2])
                l = float(ohlcv[0][3])
                self._check_mock_orders_tp_sl(self.symbol, h, l, candle_ts_ms)
        except Exception as e:
            logger.warning(f"⚠️ [SilentMonitor] {self.config_id} 检测失败: {e}")

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
                # 账户余额获取逻辑：实盘使用 total (包含持仓保证金和未实现盈亏的一部分，即钱包余额)
                # 总权益 = total + unrealized_pnl (标准合约计算公式)
                usdt_total = 0
                if 'USDT' in balance_info:
                    usdt_total = float(balance_info['USDT'].get('total', 0))
                elif 'usdt' in balance_info:
                    usdt_total = float(balance_info['usdt'].get('total', 0))
                elif 'total' in balance_info and 'USDT' in balance_info['total']:
                    usdt_total = float(balance_info['total'].get('USDT', 0))
                
                status_data["balance"] = usdt_total
                
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
                    # 1. 获取常规挂单 (LIMIT, etc.)
                    regular_orders = self.exchange.fetch_open_orders(symbol)
                    
                    # 2. 获取条件委托/触发单 (STOP_MARKET, etc.)
                    trigger_orders = []
                    try:
                        trigger_orders = self.exchange.fetch_open_orders(symbol, params={'trigger': True})
                    except Exception as te:
                        logger.warning(f"Fetch trigger orders error: {te}")
                    
                    # 合并订单
                    all_orders = regular_orders + trigger_orders
                    
                    logger.info(f"[{symbol}] Fetched {len(regular_orders)} regular + {len(trigger_orders)} trigger orders")
                    for i, o in enumerate(all_orders):
                        logger.info(f"  Order #{i}: ID={o.get('id')} Type={o.get('type')} Status={o.get('status')} Price={o.get('price')} StopPrice={o.get('stopPrice')} Amount={o.get('amount')} RawInfo={o.get('info')}")
                    
                    filtered_orders = []
                    for o in all_orders:
                        # 从 exchange 的原始响应中提取 positionSide (针对币安 USDM)
                        # ccxt 统一结构中通常在 o['info']['positionSide']
                        info = o.get('info', {})
                        pos_side = info.get('positionSide', 'BOTH')
                        
                        # 核心修复：处理条件单 (STOP_MARKET, TAKE_PROFIT_MARKET 等)
                        # 这些订单在 fetch_open_orders 中 price 为 0 或 None，实际价格在 stopPrice 中
                        order_type = o.get('type', '').upper()
                        
                        # 安全转换 float
                        def safe_float(val, default=0.0):
                            try:
                                if val is None: return default
                                return float(val)
                            except:
                                return default

                        price = safe_float(o.get('price'))
                        # 尝试从多个地方获取数量：ccxt 标准字段 -> info['origQty'] -> info['qty']
                        amount = safe_float(o.get('amount') or info.get('origQty') or info.get('qty'))
                        
                        # 特殊处理：如果是全平委托 (closePosition: true)，数量会显示为 0
                        # 此时我们需要从持仓中找到实际对应的数量，否则 Agent 看到数量为 0 会误判从而撤销并重新挂单
                        if amount == 0 and str(info.get('closePosition', '')).lower() == 'true':
                            for pos in status_data.get("real_positions", []):
                                if pos["side"] == pos_side.upper():
                                    amount = pos["amount"]
                                    break
                        
                        # 尝试从多个位置获取触发价 (stopPrice)
                        stop_price = safe_float(o.get('stopPrice') or info.get('stopPrice') or info.get('triggerPrice'))
                        
                        # 如果是条件单且价格为0，则使用触发价作为显示价格
                        if price == 0 and stop_price > 0:
                            price = stop_price
                        
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
                            'amount': amount,
                            'status': o.get('status')
                        })
                    status_data["real_open_orders"] = filtered_orders
                except Exception as e:
                    logger.warning(f"Fetch real orders error: {e}")
            else:
                # 模拟模式
                # 获取策略沙盒内的资金（破产会自动重置回10000并在数据库记1笔failures）
                mock_acc = database.get_mock_account(config_id or agent_name, symbol)
                status_data["balance"] = mock_acc.get('balance', 10000.0)
                
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

    # VP 回看长度按周期自适应
    VP_LENGTH_MAP = {
        '5m': 576,   # ~2 天
        '15m': 384,  # ~4 天
        '1h': 360,   # ~15 天
        '4h': 180,   # ~30 天
        '1d': 120,   # ~4 个月
        '1w': 52,    # ~1 年
    }

    # VWAP 仅在日内周期有效
    VWAP_VALID_TFS = {'1m', '5m', '15m', '30m', '1h'}

    def process_timeframe(self, symbol, tf):
        """
        处理单个时间周期的核心逻辑（精简优化版 v2）
        移除冗余指标 (StochRSI/KDJ/CCI/EMA100)，新增 MACD 动量标注与 RSI 背离检测
        """
        try:
            logger.debug(f"    🔍 [{tf}] Fetching OHLCV data for {symbol}...")
            ohlcv = self.exchange.fetch_ohlcv(symbol, tf, limit=1000)
            if not ohlcv or len(ohlcv) < 200:
                logger.warning(f"    ⚠️ [{tf}] Insufficient OHLCV data: {len(ohlcv) if ohlcv else 0} candles (need >= 200)")
                return None

            logger.debug(f"    ✅ [{tf}] Got {len(ohlcv)} candles, calculating indicators...")
            df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            df['time'] = pd.to_datetime(df['time'], unit='ms')
            
            close = df['close']
            high = df['high']
            low = df['low']
            volume = df['volume']
            
            # Note: Removed redundant _check_mock_orders_tp_sl call here because it was using 
            # the entire timeframe's high/low (e.g. 1d high/low) which is incorrect for 
            # real-time monitoring. Monitoring is now handled by run_silent_sl_tp() 
            # in main_scheduler.py on a 1-minute basis.
            
            # ================= 精简指标计算 =================
            # 1. 均线 (移除 EMA100，保留 20/50/200)
            ema20 = calc_ema(close, 20)
            ema50 = calc_ema(close, 50)
            ema200 = calc_ema(close, 200)
            
            # 2. 动量 (仅保留 RSI，移除 StochRSI/KDJ/CCI)
            rsi = calc_rsi(close, 14)
            atr = calc_atr(df, 14)
            macd_line, signal_line, hist = calc_macd(close)
            
            # 3. 趋势强度
            adx, plus_di, minus_di = calc_adx(df)
            
            # 4. VWAP (仅日内周期)
            vwap_val = None
            if tf in self.VWAP_VALID_TFS:
                vwap = calc_vwap(df)
                vwap_val = smart_fmt(vwap.iloc[-1])
            
            # 5. 布林带
            bb_up, bb_mid, bb_low, bb_width = calc_bollinger_bands(close)
            
            # 6. 成交量分析
            vol_ma20 = volume.rolling(window=20).mean()
            vol_ratio = (volume / vol_ma20).fillna(0)
            
            # 7. VP 分布 (自适应回看长度)
            vp_length = self.VP_LENGTH_MAP.get(tf, 360)
            vp = calculate_vp(df, length=vp_length)
            if not vp: vp = {"poc": 0, "vah": 0, "val": 0, "hvns": []}
            
            # ================= 新增：MACD Hist 动量标注 =================
            hist_prev = float(hist.iloc[-2])
            hist_curr = float(hist.iloc[-1])
            if hist_curr > 0 and hist_curr > hist_prev:
                macd_momentum = "多头加速"
            elif hist_curr > 0 and hist_curr <= hist_prev:
                macd_momentum = "多头减速 ⚠️"
            elif hist_curr < 0 and hist_curr < hist_prev:
                macd_momentum = "空头加速"
            elif hist_curr < 0 and hist_curr >= hist_prev:
                macd_momentum = "空头减速 ⚠️"
            else:
                macd_momentum = "零轴附近"
            
            # ================= 新增：RSI 背离检测 =================
            rsi_divergence = detect_rsi_divergence(close, rsi, lookback=20)
            
            # ================= 提取最新值 =================
            curr_close = close.iloc[-1]
            e20_val = ema20.iloc[-1]
            e50_val = ema50.iloc[-1]
            e200_val = ema200.iloc[-1]
            
            # 趋势判定
            trend_status = "Consolidation"
            if e20_val > e50_val > e200_val: trend_status = "Strong Uptrend"
            elif e20_val < e50_val < e200_val: trend_status = "Strong Downtrend"
            elif curr_close > e200_val: trend_status = "Bullish Neutral"
            elif curr_close < e200_val: trend_status = "Bearish Neutral"

            adx_val = float(adx.iloc[-1])
            trend_strength = "Strong" if adx_val > 25 else "Weak/Ranging"
            
            # 序列数据
            def to_list(series, n=10):
                raw = series.iloc[-n:].values.tolist()
                return [smart_fmt(float(x)) for x in raw]

            recent_opens = to_list(df['open'])
            recent_closes = to_list(close)
            recent_highs = to_list(high)
            recent_lows = to_list(low)

            # ================= 构建精简结果 =================
            rsi_val = round(float(rsi.iloc[-1]), 1)
            rsi_result = {"rsi": rsi_val}
            if rsi_divergence:
                rsi_result["divergence"] = rsi_divergence

            result = {
                "price": smart_fmt(curr_close),
                "trend": {
                    "status": trend_status,
                    "strength": trend_strength,
                    "adx": round(adx_val, 1),
                    "di_plus": round(float(plus_di.iloc[-1]), 1),
                    "di_minus": round(float(minus_di.iloc[-1]), 1),
                },
                "recent_opens": recent_opens,
                "recent_closes": recent_closes,
                "recent_highs": recent_highs,
                "recent_lows": recent_lows,

                "rsi_analysis": rsi_result,

                "atr": smart_fmt(atr.iloc[-1]),
                "macd": {
                    "diff": smart_fmt(macd_line.iloc[-1]),
                    "dea": smart_fmt(signal_line.iloc[-1]),
                    "hist": smart_fmt(hist_curr),
                    "momentum": macd_momentum,
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
                    "ema_200": smart_fmt(e200_val)
                },

                "volume_analysis": {
                    "current": smart_fmt(volume.iloc[-1]),
                    "ratio": round(float(vol_ratio.iloc[-1]), 2),
                    "status": "High" if float(vol_ratio.iloc[-1]) > 1.5 else ("Low" if float(vol_ratio.iloc[-1]) < 0.5 else "Normal")
                },

                "vp": vp
            }

            # VWAP 仅在日内周期输出
            if vwap_val is not None:
                result["vwap"] = vwap_val

            logger.debug(f"    ✅ [{tf}] Indicators calculated (price={result['price']}, atr={result['atr']}, momentum={macd_momentum})")
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
                    
                    # 优先尝试普通撤单
                    try:
                        res = self.exchange.cancel_order(cancel_id, symbol)
                        logger.info(f"✅ [CANCEL] 普通撤单成功: {cancel_id}")
                        return {"status": "cancelled", "response": res}
                    except Exception as e:
                        # 如果报错 OrderNotFound，可能是条件单 (Trigger Order / Algo Order)
                        logger.warning(f"⚠️ [CANCEL] 普通撤单失败或未找到订单，尝试条件单撤单模式: {e}")
                        try:
                            # 针对币安合约的条件单（如止损单），需加 params={'trigger': True} 或 {'stop': True}
                            # CCXT 统一建议使用 trigger: True
                            res = self.exchange.cancel_order(cancel_id, symbol, params={'trigger': True})
                            logger.info(f"✅ [CANCEL] 条件单撤单成功: {cancel_id}")
                            return {"status": "cancelled", "response": res}
                        except Exception as e2:
                            logger.error(f"❌ [CANCEL] 彻底撤单失败: {e2}")
                            raise e2
                else:
                    raise ValueError("CANCEL 指令缺失 cancel_order_id 参数")
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

                        params = {}
                        if self.exchange.id == 'okx':
                            params['posSide'] = side # long or short
                        else:
                            params['positionSide'] = current_pos_side_str
                        
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
                                if self.exchange.id == 'okx':
                                    params['triggerPrice'] = float(formatted_price)
                                else:
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
                    if self.exchange.id == 'okx':
                         params['posSide'] = pos_side.lower()
                    else:
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
