import pandas as pd
import numpy as np

def smart_fmt(value):
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

def calc_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def calc_rsi(series, period=14):
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

def calc_stoch_rsi(rsi, period=14, k_period=3, d_period=3):
    """计算 StochRSI"""
    rsi_min = rsi.rolling(window=period).min()
    rsi_max = rsi.rolling(window=period).max()
    stoch_rsi = (rsi - rsi_min) / (rsi_max - rsi_min)
    stoch_rsi = stoch_rsi.replace([np.inf, -np.inf], 0.5).fillna(0.5)
    fast_k = stoch_rsi.rolling(window=k_period).mean() * 100
    fast_d = fast_k.rolling(window=d_period).mean()
    return fast_k, fast_d

def calc_adx(df, period=14):
    """计算 ADX (Trend Strength)"""
    high = df['high']
    low = df['low']
    close = df['close']
    
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

def calc_vwap(df):
    """计算 VWAP (成交量加权平均价)"""
    v = df['volume']
    p = (df['high'] + df['low'] + df['close']) / 3
    vwap = (p * v).cumsum() / v.cumsum()
    return vwap.fillna(p)

def calc_cci(df, period=20):
    """计算 CCI (Commodity Channel Index)"""
    tp = (df['high'] + df['low'] + df['close']) / 3
    ma = tp.rolling(window=period).mean()
    md = tp.rolling(window=period).apply(lambda x: np.fabs(x - x.mean()).mean())
    cci = (tp - ma) / (0.015 * md)
    return cci.fillna(0)

def calc_atr(df, period=14):
    high, low, close = df['high'], df['low'], df['close']
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

def calc_macd(close, fast=12, slow=26, signal=9):
    """计算 MACD, Signal, Histogram"""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def calc_bollinger_bands(close, window=20, num_std=2):
    """计算布林带"""
    rolling_mean = close.rolling(window=window).mean()
    rolling_std = close.rolling(window=window).std()
    upper = rolling_mean + (rolling_std * num_std)
    lower = rolling_mean - (rolling_std * num_std)
    safe_mean = rolling_mean.replace(0, 1e-10)
    width = (upper - lower) / safe_mean
    width = width.replace([np.inf, -np.inf], 0.0)
    return upper, rolling_mean, lower, width

def calc_kdj(df, n=9, m1=3, m2=3):
    """计算 KDJ 指标"""
    low_list = df['low'].rolling(n).min()
    high_list = df['high'].rolling(n).max()
    diff_list = high_list - low_list
    diff_list = diff_list.replace(0, np.nan)
    rsv = pd.Series((df['close'] - low_list) / diff_list * 100, index=df.index)
    rsv = rsv.fillna(50.0)
    rsv = rsv.replace([np.inf, -np.inf], 50.0)
    k = rsv.ewm(alpha=1/m1, adjust=False).mean()
    d = k.ewm(alpha=1/m2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j

def calculate_vp(df, length=360, rows=100, va_perc=0.70):
    """
    计算体积分布 (Volume Profile) - 严格对齐 LuxAlgo 逻辑
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
    
    for i in range(len(subset)):
        h, l, v = highs[i], lows[i], vols[i]
        if h == l:
            bin_idx = min(int((h - low_val) / price_step), rows - 1)
            total_volume[bin_idx] += v
            continue
        
        start_bin = max(0, min(int((l - low_val) / price_step), rows - 1))
        end_bin = max(0, min(int((h - low_val) / price_step), rows - 1))
        
        price_range = h - l
        vol_per_price = v / price_range if price_range != 0 else 0
        
        for b in range(start_bin, end_bin + 1):
            bin_low = low_val + b * price_step
            bin_high = low_val + (b + 1) * price_step
            overlap = max(0, min(h, bin_high) - max(l, bin_low))
            total_volume[b] += overlap * vol_per_price

    poc_idx = np.argmax(total_volume)
    poc_price = low_val + (poc_idx + 0.5) * price_step
    
    total_traded_vol = np.sum(total_volume)
    target_vol = total_traded_vol * va_perc
    current_vol = total_volume[poc_idx]
    vah_idx = poc_idx
    val_idx = poc_idx
    
    while current_vol < target_vol:
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
    
    hvns = []
    detection_percent = 0.09 
    neighbor_n = int(rows * detection_percent)
    if neighbor_n < 1: neighbor_n = 1
    threshold_vol = np.max(total_volume) * 0.01

    for i in range(neighbor_n, rows - neighbor_n):
        curr_vol = total_volume[i]
        if curr_vol < threshold_vol: continue
        is_peak = True
        for offset in range(1, neighbor_n + 1):
            if total_volume[i - offset] >= curr_vol or total_volume[i + offset] >= curr_vol:
                is_peak = False
                break
        if is_peak:
            hvns.append(low_val + (i + 0.5) * price_step)
    
    if not hvns: hvns.append(poc_price)

    return {
        "poc": smart_fmt(poc_price), 
        "vah": smart_fmt(vah_price), 
        "val": smart_fmt(val_price),
        "hvns": [smart_fmt(x) for x in sorted(hvns, reverse=True)]
    }
