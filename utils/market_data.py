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
            return round(val, 8) 

    def _calc_ema(self, series, span):
        return series.ewm(span=span, adjust=False).mean()

    def _calc_rsi(self, series, period=14):
        delta = series.diff()
        gain = (delta.where(delta > 0, 0))
        loss = (-delta.where(delta < 0, 0))
        avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        avg_loss = avg_loss.replace(0, 1e-10)
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.replace([np.inf, -np.inf], 50.0)
        return rsi

    def _calc_stoch_rsi(self, rsi, period=14, k_period=3, d_period=3):
        """计算 StochRSI"""
        rsi_min = rsi.rolling(window=period).min()
        rsi_max = rsi.rolling(window=period).max()
        stoch_rsi = (rsi - rsi_min) / (rsi_max - rsi_min)
        stoch_rsi = stoch_rsi.replace([np.inf, -np.inf], 0.5).fillna(0.5)
        fast_k = stoch_rsi.rolling(window=k_period).mean() * 100
        fast_d = fast_k.rolling(window=d_period).mean()
        return fast_k, fast_d

    def _calc_adx(self, df, period=14):
        """计算 ADX (Trend Strength)"""
        high = df['high']
        low = df['low']
        close = df['close']
        
        # Wilder's ADX: avoid look-ahead and smooth with RMA (EMA alpha=1/period)
        up_move = high.diff()
        down_move = low.shift(1) - low

        plus_dm = pd.Series(
            np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
            index=df.index,
        )
        minus_dm = pd.Series(
            np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
            index=df.index,
        )

        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

        plus_di = 100 * plus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr
        minus_di = 100 * minus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr
        di_sum = (plus_di + minus_di).replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / di_sum
        adx = dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        return adx.fillna(0.0), plus_di.fillna(0.0), minus_di.fillna(0.0)

    def _calc_vwap(self, df):
        """计算 VWAP (成交量加权平均价)"""
        # 简单实现：按整个数据集周期计算 (如果是日内分析比较准确)
        v = df['volume']
        p = (df['high'] + df['low'] + df['close']) / 3
        vwap = (p * v).cumsum() / v.cumsum()
        return vwap.fillna(p)

    def _calc_cci(self, df, period=20):
        """计算 CCI (Commodity Channel Index)"""
        tp = (df['high'] + df['low'] + df['close']) / 3
        ma = tp.rolling(window=period).mean()
        md = tp.rolling(window=period).apply(lambda x: np.fabs(x - x.mean()).mean())
        cci = (tp - ma) / (0.015 * md)
        return cci.fillna(0)

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
        # 防止除零错误：当中轨为0时的处理
        safe_mean = rolling_mean.replace(0, 1e-10)
        width = (upper - lower) / safe_mean
        # 处理可能的无穷大值
        width = width.replace([np.inf, -np.inf], 0.0)
        return upper, rolling_mean, lower, width

    def _calc_kdj(self, df, n=9, m1=3, m2=3):
        """计算 KDJ 指标"""
        low_list = df['low'].rolling(n).min()
        high_list = df['high'].rolling(n).max()
        # 防止除零错误：当高低区间为0时，设置RSV为50（中间值）
        diff_list = high_list - low_list
        # 替换分母为0的情况，避免除零错误
        diff_list = diff_list.replace(0, np.nan)
        rsv = pd.Series((df['close'] - low_list) / diff_list * 100, index=df.index)
        # 将可能的无穷大值或NaN值替换为50
        rsv = rsv.fillna(50.0)
        rsv = rsv.replace([np.inf, -np.inf], 50.0)
        k = rsv.ewm(alpha=1/m1, adjust=False).mean()
        d = k.ewm(alpha=1/m2, adjust=False).mean()
        j = 3 * k - 2 * d
        return k, d, j

    def _calculate_vp(self, df, length=360, rows=100, va_perc=0.70):
        """
        计算体积分布 (Volume Profile) - 严格对齐 LuxAlgo 逻辑
        LuxAlgo Logic: Peak detection uses N-neighbors comparison.
        """
        if len(df) < 50: return None
        
        # 1. 数据截取
        subset = df.iloc[-length:].copy().reset_index(drop=True)
        high_val = subset['high'].max()
        low_val = subset['low'].min()
        
        if high_val == low_val: return None
        
        # 2. 构建桶 (Bins)
        price_step = (high_val - low_val) / rows
        total_volume = np.zeros(rows)
        
        highs = subset['high'].values
        lows = subset['low'].values
        vols = subset['volume'].values
        
        # 3. 分配成交量 (Uniform Distribution Assumption)
        # 注意: 纯 Python 无法获取 Lower Timeframe 数据，这里假设 K 线内成交量均匀分布
        for i in range(len(subset)):
            h, l, v = highs[i], lows[i], vols[i]
            if h == l:
                bin_idx = min(int((h - low_val) / price_step), rows - 1)
                total_volume[bin_idx] += v
                continue
            
            start_bin = max(0, min(int((l - low_val) / price_step), rows - 1))
            end_bin = max(0, min(int((h - low_val) / price_step), rows - 1))
            
            # 防止除以零
            price_range = h - l
            if price_range == 0: 
                vol_per_price = 0
            else:
                vol_per_price = v / price_range
            
            for b in range(start_bin, end_bin + 1):
                bin_low = low_val + b * price_step
                bin_high = low_val + (b + 1) * price_step
                # 计算 K线 与 当前桶 的重叠高度
                overlap = max(0, min(h, bin_high) - max(l, bin_low))
                total_volume[b] += overlap * vol_per_price

        # 4. 计算 POC
        poc_idx = np.argmax(total_volume)
        poc_price = low_val + (poc_idx + 0.5) * price_step
        
        # 5. 计算 Value Area (VA)
        total_traded_vol = np.sum(total_volume)
        target_vol = total_traded_vol * va_perc
        
        current_vol = total_volume[poc_idx]
        vah_idx = poc_idx
        val_idx = poc_idx
        
        # 严格按照从 POC 向两边扩展的逻辑
        while current_vol < target_vol:
            # 边界检查
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
        
        # 6. 计算 HVN (High Volume Nodes) - 匹配 LuxAlgo "Peaks" 逻辑
        # LuxAlgo Default: vn_peaksNumberOfNodes = 9% (of rows)
        # 也就是左右各 N 个节点必须小于当前节点
        hvns = []
        
        # LuxAlgo 默认 9% 的行数作为检测窗口
        detection_percent = 0.09 
        neighbor_n = int(rows * detection_percent)
        if neighbor_n < 1: neighbor_n = 1
        
        # 阈值：LuxAlgo 默认为 max volume 的 1%
        threshold_vol = np.max(total_volume) * 0.01

        for i in range(neighbor_n, rows - neighbor_n):
            curr_vol = total_volume[i]
            
            # 基础阈值过滤
            if curr_vol < threshold_vol:
                continue

            is_peak = True
            
            # 检查左边 N 个
            # LuxAlgo 逻辑: if tempPeakTotalVolume.get(volumeNodeLevel - peaksNumberOfNodes) <= tempPeakTotalVolume.get(currentVolumeNode) -> peakUpperNth = false
            # 意味着：当前节点必须 > 周围节点 (严格大于或大于等于视实现而定，通常找局部极大值)
            for offset in range(1, neighbor_n + 1):
                if total_volume[i - offset] >= curr_vol:
                    is_peak = False
                    break
                if total_volume[i + offset] >= curr_vol:
                    is_peak = False
                    break
            
            if is_peak:
                hvns.append(low_val + (i + 0.5) * price_step)
        
        # 如果没有检测到 HVN，至少放入 POC
        if not hvns:
            hvns.append(poc_price)

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
                status_data["balance"] = float(balance_info.get('USDT', {}).get('free', 0))
                
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
                    all_orders = self.exchange.fetch_open_orders(symbol)
                    filtered_orders = []
                    for o in all_orders:
                        # 从 exchange 的原始响应中提取 positionSide (针对币安 USDM)
                        # ccxt 统一结构中通常在 o['info']['positionSide']
                        pos_side = o.get('info', {}).get('positionSide', 'BOTH')
                        
                        filtered_orders.append({
                            'order_id': str(o.get('id')),
                            'side': o.get('side', '').lower(),
                            'pos_side': pos_side.upper(), # 'LONG', 'SHORT' or 'BOTH'
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
        
        logger.info(f"Fetching {symbol} market data ({mode} mode: {timeframes})...")
        
        for tf in timeframes:
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
            stoch_k, stoch_d = self._calc_stoch_rsi(rsi)
            atr = self._calc_atr(df, 14)
            macd, signal, hist = self._calc_macd(close)
            k, d, j = self._calc_kdj(df)
            cci = self._calc_cci(df)
            
            # 3. 趋势强度 (ADX) 与 价值中枢 (VWAP)
            adx, plus_di, minus_di = self._calc_adx(df)
            vwap = self._calc_vwap(df)
            
            # 4. 布林带 (判断波动率挤压)
            bb_up, bb_mid, bb_low, bb_width = self._calc_bollinger_bands(close)
            
            # 5. 成交量分析
            vol_ma20 = volume.rolling(window=20).mean()
            vol_ratio = (volume / vol_ma20).fillna(0)
            
            # 6. VP 分布
            vp = self._calculate_vp(df, length=360)
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
                return [self._smart_fmt(float(x)) for x in raw]

            recent_closes = to_list(close)
            recent_highs = to_list(high)
            recent_lows = to_list(low)
            
            return {
                "price": self._smart_fmt(curr_close),
                "trend": {
                    "status": trend_status,
                    "strength": trend_strength,
                    "adx": round(adx_val, 1)
                },
                "vwap": self._smart_fmt(vwap.iloc[-1]),
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

                "ema": {
                    "ema_20": self._smart_fmt(e20_val),
                    "ema_50": self._smart_fmt(e50_val),
                    "ema_100": self._smart_fmt(ema100.iloc[-1]),
                    "ema_200": self._smart_fmt(e200_val)
                },
                
                "volume_analysis": {
                    "current": self._smart_fmt(volume.iloc[-1]),
                    "ratio": round(float(vol_ratio.iloc[-1]), 2),
                    "status": "High" if float(vol_ratio.iloc[-1]) > 1.5 else ("Low" if float(vol_ratio.iloc[-1]) < 0.5 else "Normal")
                },
                
                "vp": vp
            }
        except Exception as e:
            logger.error(f"Process TF Error {tf}: {e}")
            import traceback
            traceback.print_exc()
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
                executed = False

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
                                self.exchange.create_order(symbol, order_type, close_side, final_amt, None, params=params)

                            else:
                                logger.info(f"💰 [CLOSE-TP] 检测到止盈场景 (现价 {current_price} -> 目标 {formatted_price})")
                                order_type = 'LIMIT'
                                params['timeInForce'] = 'GTC'
                                self.exchange.create_order(symbol, order_type, close_side, final_amt, float(formatted_price), params=params)


                            # 1. 限价平仓 (Limit Close)
                            # order_type = 'LIMIT'
                            # formatted_price = self.exchange.price_to_precision(symbol, raw_close_price)
                            # params['timeInForce'] = 'GTC' # 限价单需要 GTC
                            
                            # logger.info(f"🚀 [CLOSE-LIMIT] 下单: {current_pos_side_str} -> {close_side} {formatted_amt} @ {formatted_price}")
                            # self.exchange.create_order(symbol, order_type, close_side, final_amt, float(formatted_price), params=params)
                        else:
                            # 2. 市价平仓 (Market Close)
                            order_type = 'MARKET'
                            logger.info(f"🚀 [CLOSE-MARKET] 下单: {current_pos_side_str} -> {close_side} {formatted_amt} @ 市价")
                            self.exchange.create_order(symbol, order_type, close_side, final_amt, params=params)
                        
                        executed = True

                if executed:
                    logger.info(f"✅ [CLOSE] 平仓指令执行完毕")
                    return {"status": "closed"}
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

        except Exception as e:
            logger.error(f"❌ [ORDER_ERROR] 执行异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
            
    def fetch_recent_trades(self, symbol, limit=20):
        try:
            return self.exchange.fetch_my_trades(symbol, limit=limit)
        except:
            return []
