import pandas as pd
import numpy as np

def smart_fmt(value):
    """Keep eight significant digits without turning missing or tiny values into zero."""
    if value is None or pd.isna(value):
        return None
    val = float(value)
    return float(f"{val:.8g}") if np.isfinite(val) else None

def calc_ema(series, span):
    # Recursive EMA seeded by the first close; insufficient warm-up is reported by callers.
    return series.ewm(span=span, adjust=False).mean()

def calc_emas(series, spans=(20, 50, 100, 200)):
    """Calculate multiple EMA spans in one compact helper."""
    return {int(span): calc_ema(series, int(span)) for span in spans}

def wilder_rma(series, period=14):
    """SMA-seeded Wilder smoothing; missing values restart the warm-up window."""
    if period < 1:
        raise ValueError("period must be positive")
    values = pd.to_numeric(series, errors='coerce').to_numpy(dtype=float)
    result = np.full(len(values), np.nan)
    seed = []
    previous = np.nan
    for index, value in enumerate(values):
        if not np.isfinite(value):
            seed = []
            previous = np.nan
            continue
        if np.isnan(previous):
            seed.append(value)
            if len(seed) < period:
                continue
            previous = float(np.mean(seed))
        else:
            previous = (previous * (period - 1) + value) / period
        result[index] = previous
    return pd.Series(result, index=series.index)


def calc_rsi(series, period=14):
    """Wilder RSI: unknown during warm-up, neutral for a truly flat series."""
    delta = series.diff()
    gain = wilder_rma(delta.clip(lower=0), period)
    loss = wilder_rma(-delta.clip(upper=0), period)
    rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    rsi = rsi.mask((loss == 0) & (gain > 0), 100.0)
    return rsi.mask((loss == 0) & (gain == 0), 50.0)


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
    atr = wilder_rma(tr, period)

    plus_di = 100 * wilder_rma(plus_dm, period) / atr
    minus_di = 100 * wilder_rma(minus_dm, period) / atr
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = (100 * (plus_di - minus_di).abs() / di_sum).mask(di_sum.isna() & atr.notna(), 0.0)
    adx = wilder_rma(dx, period)
    return adx, plus_di.mask(atr == 0, 0.0), minus_di.mask(atr == 0, 0.0)

def calc_vwap(df):
    """UTC session VWAP when candle timestamps exist; otherwise explicit window VWAP."""
    v = df['volume']
    p = (df['high'] + df['low'] + df['close']) / 3
    if "time" in df:
        sessions = pd.to_datetime(df["time"], utc=True).dt.floor("D")
        vwap = (p * v).groupby(sessions).cumsum() / v.groupby(sessions).cumsum().replace(0, np.nan)
    else:
        vwap = (p * v).cumsum() / v.cumsum().replace(0, np.nan)
    # A session with no traded volume has no volume-weighted price.
    return vwap

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
    return wilder_rma(tr, period)

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
    rolling_std = close.rolling(window=window).std(ddof=0)
    upper = rolling_mean + (rolling_std * num_std)
    lower = rolling_mean - (rolling_std * num_std)
    safe_mean = rolling_mean.replace(0, 1e-10)
    width = (upper - lower) / safe_mean
    width = width.replace([np.inf, -np.inf], 0.0)
    return upper, rolling_mean, lower, width


def calc_choppiness(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """CHOP measures path congestion, not direction; a zero range is undefined."""
    if period <= 1:
        raise ValueError("CHOP period must be greater than one")
    true_range = calc_atr(df, 1)
    price_range = df['high'].rolling(period).max() - df['low'].rolling(period).min()
    ratio = true_range.rolling(period).sum() / price_range.where(price_range > 0)
    return 100 * np.log10(ratio.where(ratio > 0)) / np.log10(period)


def calc_cmf(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """OHLCV pressure proxy, not actual net capital flow; flat bars contribute zero."""
    price_range = df['high'] - df['low']
    multiplier = ((2 * df['close'] - df['high'] - df['low']) / price_range.where(price_range > 0))
    multiplier = multiplier.mask(price_range == 0, 0.0)
    volume_sum = df['volume'].rolling(period).sum()
    return (multiplier * df['volume']).rolling(period).sum() / volume_sum.where(volume_sum > 0)


def calc_squeeze_state(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """BB SMA/2 population std vs KC SMA/1.5 SMA(TR), without a second momentum oscillator."""
    upper, middle, lower, _ = calc_bollinger_bands(df['close'], period, 2)
    range_mean = calc_atr(df, 1).rolling(period).mean()
    kc_upper, kc_lower = middle + 1.5 * range_mean, middle - 1.5 * range_mean
    result = pd.Series('unavailable', index=df.index, dtype=object)
    valid = upper.notna() & lower.notna() & (range_mean > 0)
    result.loc[valid] = 'neutral'
    result.loc[valid & (lower > kc_lower) & (upper < kc_upper)] = 'on'
    result.loc[valid & (lower < kc_lower) & (upper > kc_upper)] = 'off'
    return result


def build_market_regime_context(df: pd.DataFrame) -> dict:
    """Compact observations; durations are bounded by the available calculation history."""
    chop, cmf, squeeze = calc_choppiness(df), calc_cmf(df), calc_squeeze_state(df)

    def suffix_count(values):
        current = values.iloc[-1]
        if current == 'unavailable':
            return None
        count = 0
        for value in reversed(values.tolist()):
            if value != current:
                break
            count += 1
        return count

    def change(series):
        return smart_fmt(series.iloc[-1] - series.iloc[-4]) if len(series) >= 4 else None

    chop_states = chop.map(lambda value: 'unavailable' if pd.isna(value) else
                           'choppy' if value > 61.8 else 'trending' if value < 38.2 else 'transition')
    cmf_signs = cmf.map(lambda value: 'unavailable' if pd.isna(value) else
                       'positive' if value > 0 else 'negative' if value < 0 else 'neutral')
    release_age = None
    previous = 'unavailable'
    for state in squeeze:
        if state in {'on', 'unavailable'}:
            release_age = None
        elif previous == 'on' and state == 'off':
            release_age = 0
        elif release_age is not None:
            release_age += 1
        previous = state
    return {
        'chop': {'value': smart_fmt(chop.iloc[-1]), 'change_3_bars': change(chop),
                 'state': chop_states.iloc[-1], 'state_bars': suffix_count(chop_states)},
        'cmf': {'value': smart_fmt(cmf.iloc[-1]), 'change_3_bars': change(cmf),
                'sign': cmf_signs.iloc[-1], 'sign_bars': suffix_count(cmf_signs)},
        'squeeze': {'state': squeeze.iloc[-1],
                    'on_bars': (suffix_count(squeeze) if squeeze.iloc[-1] == 'on' else
                                None if squeeze.iloc[-1] == 'unavailable' else 0),
                    'bars_since_release': release_age},
    }


def build_agent_indicator_context(
    df: pd.DataFrame,
    emas: dict[int, pd.Series],
    rsi: pd.Series,
    atr: pd.Series,
    macd_hist: pd.Series,
) -> dict:
    """Describe closed-bar changes in comparable units; these are observations, not forecasts."""
    close = df['close']
    price = float(close.iloc[-1])
    atr_value = float(atr.iloc[-1])

    def rounded(value, digits=3):
        return round(float(value), digits) if pd.notna(value) and np.isfinite(value) else None

    def in_atr(value):
        return rounded(value / atr_value) if np.isfinite(atr_value) and atr_value > 0 else None

    def change_pct(series, bars):
        if len(series) <= bars or series.iloc[-bars - 1] <= 0:
            return None
        return rounded((series.iloc[-1] / series.iloc[-bars - 1] - 1) * 100)

    prior_range = {}
    if len(df) >= 21:
        previous = df.iloc[-21:-1]
        previous_high = float(previous['high'].max())
        previous_low = float(previous['low'].min())
        prior_range = {
            'high': smart_fmt(previous_high),
            'low': smart_fmt(previous_low),
            # Signed distances: a negative distance means price has crossed that edge.
            'to_high_atr': in_atr(previous_high - price),
            'to_low_atr': in_atr(price - previous_low),
        }

    return {
        'regime': build_market_regime_context(df),
        'trend': {
            'price_minus_ema20_atr': in_atr(price - emas[20].iloc[-1]) if len(df) >= 20 else None,
            'ema20_minus_ema50_atr': in_atr(emas[20].iloc[-1] - emas[50].iloc[-1]) if len(df) >= 50 else None,
            'ema20_change_3_bars_pct': change_pct(emas[20], 3) if len(df) >= 23 else None,
        },
        'momentum': {
            'return_1_bar_pct': change_pct(close, 1),
            'return_3_bars_pct': change_pct(close, 3),
            'return_15_bars_pct': change_pct(close, 15),
            'rsi_change_3_bars': rounded(rsi.iloc[-1] - rsi.iloc[-4], 2) if len(df) >= 18 else None,
            'macd_hist_atr': in_atr(macd_hist.iloc[-1]) if len(df) >= 35 else None,
            'macd_hist_change_1_bar_atr': in_atr(macd_hist.iloc[-1] - macd_hist.iloc[-2]) if len(df) >= 36 else None,
        },
        'volatility': {
            'atr_pct': rounded(atr_value / price * 100),
            'last_range_atr': in_atr(float(df['high'].iloc[-1] - df['low'].iloc[-1])),
        },
        'prior_range_20': prior_range,
    }


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
    Estimate a volume profile by uniformly allocating each candle's volume across its range.
    """
    if len(df) < 50: return None
    
    subset = df.iloc[-length:].copy().reset_index(drop=True)
    high_val = subset['high'].max()
    low_val = subset['low'].min()
    
    if high_val == low_val or subset['volume'].sum() <= 0: return None
    
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


def _pivot_points(series, size=5, kind="high"):
    if len(series) < size * 2 + 1:
        return []

    pivots = []
    values = series.values
    for idx in range(size, len(series) - size):
        window = values[idx - size:idx + size + 1]
        value = values[idx]
        if kind == "high" and value == np.max(window):
            pivots.append({"index": idx, "confirmed_index": idx + size, "price": float(value)})
        elif kind == "low" and value == np.min(window):
            pivots.append({"index": idx, "confirmed_index": idx + size, "price": float(value)})
    return pivots


def _find_order_block(df, start_idx, end_idx, bias):
    if end_idx <= start_idx:
        return None

    subset = df.iloc[start_idx:end_idx + 1]
    if subset.empty:
        return None

    if bias == "bullish":
        bearish = subset[subset["close"] < subset["open"]]
        candle = bearish.iloc[-1] if not bearish.empty else subset.loc[subset["low"].idxmin()]
    else:
        bullish = subset[subset["close"] > subset["open"]]
        candle = bullish.iloc[-1] if not bullish.empty else subset.loc[subset["high"].idxmax()]

    return {
        "bias": bias,
        "low": smart_fmt(float(candle["low"])),
        "high": smart_fmt(float(candle["high"])),
    }


def _unmitigated_order_blocks(df, events, limit=3):
    blocks = []
    for event in reversed(events):
        block = event.get("order_block")
        if not block:
            continue
        created_idx = event.get("index", 0)
        later = df.iloc[created_idx + 1:]
        if block["bias"] == "bullish":
            mitigated = not later.empty and float(later["low"].min()) < float(block["low"])
        else:
            mitigated = not later.empty and float(later["high"].max()) > float(block["high"])
        block_payload = {**block, "mitigated": bool(mitigated), **_level_metadata(df, created_idx, block)}
        if not mitigated:
            if not any((b['low'], b['high'], b['bias']) == (block['low'], block['high'], block['bias']) for b in blocks):
                blocks.append(block_payload)
        if len(blocks) >= limit:
            break
    return blocks


def _fair_value_gaps(df, limit=3, include_mitigated=False):
    gaps = []
    for idx in range(2, len(df)):
        prev2 = df.iloc[idx - 2]
        curr = df.iloc[idx]
        if float(curr["low"]) > float(prev2["high"]):
            gap = {
                "bias": "bullish",
                "low": smart_fmt(float(prev2["high"])),
                "high": smart_fmt(float(curr["low"])),
                "index": idx,
            }
            later = df.iloc[idx + 1:]
            consumed = 0.0
            mitigated = False
            if not later.empty:
                min_low = float(later["low"].min())
                mitigated = min_low <= float(gap["low"])
                consumed = max(0.0, min(1.0, (float(gap["high"]) - min_low) / max(float(gap["high"]) - float(gap["low"]), 1e-10)))
            gap["mitigated"] = mitigated
            gap["consumed_pct"] = round(consumed * 100, 1)
            if include_mitigated or not mitigated:
                gaps.append(gap)
        if float(curr["high"]) < float(prev2["low"]):
            gap = {
                "bias": "bearish",
                "low": smart_fmt(float(curr["high"])),
                "high": smart_fmt(float(prev2["low"])),
                "index": idx,
            }
            later = df.iloc[idx + 1:]
            consumed = 0.0
            mitigated = False
            if not later.empty:
                max_high = float(later["high"].max())
                mitigated = max_high >= float(gap["high"])
                consumed = max(0.0, min(1.0, (max_high - float(gap["low"])) / max(float(gap["high"]) - float(gap["low"]), 1e-10)))
            gap["mitigated"] = mitigated
            gap["consumed_pct"] = round(consumed * 100, 1)
            if include_mitigated or not mitigated:
                gaps.append(gap)

    return [
        {**{k: v for k, v in gap.items() if k != "index"}, **_level_metadata(df, gap['index'], gap),
         "formed_index": gap['index']}
        for gap in reversed(gaps[-limit:])
    ]


def _equal_high_low(high_pivots, low_pivots, atr_value, limit=2):
    threshold = max(float(atr_value or 0) * 0.1, 1e-10)
    eqh = []
    eql = []

    for prev, curr in zip(high_pivots, high_pivots[1:]):
        if abs(curr["price"] - prev["price"]) <= threshold:
            eqh.append(smart_fmt((curr["price"] + prev["price"]) / 2))
    for prev, curr in zip(low_pivots, low_pivots[1:]):
        if abs(curr["price"] - prev["price"]) <= threshold:
            eql.append(smart_fmt((curr["price"] + prev["price"]) / 2))

    return {"eqh": eqh[-limit:], "eql": eql[-limit:]}


def _structure_events(working, high_pivots, low_pivots, scope):
    pivot_high = None
    pivot_low = None
    bias = "neutral"
    events = []
    high_iter = iter(high_pivots)
    low_iter = iter(low_pivots)
    next_high = next(high_iter, None)
    next_low = next(low_iter, None)

    for idx, row in working.iterrows():
        while next_high and next_high.get("confirmed_index", next_high["index"]) <= idx:
            pivot_high = {**next_high, "crossed": False}
            next_high = next(high_iter, None)
        while next_low and next_low.get("confirmed_index", next_low["index"]) <= idx:
            pivot_low = {**next_low, "crossed": False}
            next_low = next(low_iter, None)

        close_price = float(row["close"])
        if pivot_high and not pivot_high["crossed"] and close_price > pivot_high["price"]:
            tag = "CHoCH" if bias == "bearish" else "BOS"
            block = _find_order_block(working, pivot_high["index"], idx, "bullish")
            events.append({
                "index": idx,
                "scope": scope,
                "type": tag,
                "bias": "bullish",
                "level": smart_fmt(pivot_high["price"]),
                "order_block": block,
            })
            pivot_high["crossed"] = True
            bias = "bullish"

        if pivot_low and not pivot_low["crossed"] and close_price < pivot_low["price"]:
            tag = "CHoCH" if bias == "bullish" else "BOS"
            block = _find_order_block(working, pivot_low["index"], idx, "bearish")
            events.append({
                "index": idx,
                "scope": scope,
                "type": tag,
                "bias": "bearish",
                "level": smart_fmt(pivot_low["price"]),
                "order_block": block,
            })
            pivot_low["crossed"] = True
            bias = "bearish"

    return events


def _level_metadata(df, index, level):
    """Age/distance of historical structures, rather than an assertion of current relevance."""
    price = float(df['close'].iloc[-1])
    atr = float(calc_atr(df).iloc[-1]) if len(df) >= 14 else 0
    low = float(level.get('low', level.get('level', price)))
    high = float(level.get('high', level.get('level', price)))
    distance = max(low - price, price - high, 0)
    return {
        'age_bars': len(df) - 1 - index,
        'formed_at': str(df['time'].iloc[index]) if 'time' in df else None,
        'distance_atr': round(distance / atr, 2) if atr > 0 else None,
    }


def calculate_smc(df, swing_length=50, internal_length=5, atr_series=None):
    if len(df) < 20:
        return {}

    working = df.reset_index(drop=True).copy()
    size = max(2, min(int(internal_length or 5), max(2, len(working) // 8)))
    swing_size = max(3, min(int(swing_length or 50), max(3, len(working) // 4)))

    internal_highs = _pivot_points(working["high"], size=size, kind="high")
    internal_lows = _pivot_points(working["low"], size=size, kind="low")
    swing_highs = _pivot_points(working["high"], size=swing_size, kind="high")
    swing_lows = _pivot_points(working["low"], size=swing_size, kind="low")

    internal_events = _structure_events(working, internal_highs, internal_lows, "internal")
    swing_events = _structure_events(working, swing_highs, swing_lows, "swing")
    events = sorted(internal_events + swing_events, key=lambda item: item.get("index", 0))
    for event in events:
        event.update(_level_metadata(working, event['index'], event))

    if atr_series is None:
        atr_series = calc_atr(working, 14)
    atr_value = float(atr_series.iloc[-1] or 0)
    liquidity = _equal_high_low(internal_highs, internal_lows, atr_value)

    trailing_high = swing_highs[-1]["price"] if swing_highs else float(working["high"].tail(swing_size).max())
    trailing_low = swing_lows[-1]["price"] if swing_lows else float(working["low"].tail(swing_size).min())
    if trailing_high <= trailing_low:
        trailing_high = float(working["high"].tail(swing_size).max())
        trailing_low = float(working["low"].tail(swing_size).min())

    current_price = float(working["close"].iloc[-1])
    zone = "equilibrium"
    if trailing_high > trailing_low:
        premium_edge = trailing_low + (trailing_high - trailing_low) * 0.95
        discount_edge = trailing_low + (trailing_high - trailing_low) * 0.05
        eq_low = trailing_low + (trailing_high - trailing_low) * 0.475
        eq_high = trailing_low + (trailing_high - trailing_low) * 0.525
        if current_price >= premium_edge:
            zone = "premium"
        elif current_price <= discount_edge:
            zone = "discount"
        elif eq_low <= current_price <= eq_high:
            zone = "equilibrium"
        elif current_price > eq_high:
            zone = "upper_value"
        else:
            zone = "lower_value"

    latest_event = events[-1] if events else {}
    latest_internal = internal_events[-1] if internal_events else {}
    latest_swing = swing_events[-1] if swing_events else {}
    public_events = [
        {k: v for k, v in event.items() if k not in {"index", "order_block"}}
        for event in events[-5:]
    ]
    return {
        "structure": {k: v for k, v in latest_event.items() if k not in {"index", "order_block"}},
        "internal_structure": {k: v for k, v in latest_internal.items() if k not in {"index", "order_block"}},
        "swing_structure": {k: v for k, v in latest_swing.items() if k not in {"index", "order_block"}},
        "events": public_events,
        "order_blocks": _unmitigated_order_blocks(working, events),
        "liquidity": {
            **liquidity,
            "swing_high": smart_fmt(trailing_high),
            "swing_low": smart_fmt(trailing_low),
        },
        "fvg": _fair_value_gaps(working),
        "zone": {
            "name": zone,
            "high": smart_fmt(trailing_high),
            "low": smart_fmt(trailing_low),
        },
    }


def calculate_liquidity_sweep_ifvg(df, pivot_size=5, limit=3):
    """Detect liquidity sweeps plus active/inverse FVG state."""
    if len(df) < max(10, pivot_size * 2 + 3):
        return {}

    working = df.reset_index(drop=True).copy()
    size = max(2, min(int(pivot_size or 5), max(2, len(working) // 8)))
    highs = _pivot_points(working["high"], size=size, kind="high")
    lows = _pivot_points(working["low"], size=size, kind="low")

    sweeps = []
    for pivot in highs:
        later = working.iloc[pivot.get("confirmed_index", pivot["index"]) + 1:]
        for idx, row in later.iterrows():
            if float(row["high"]) > pivot["price"] and float(row["close"]) < pivot["price"]:
                sweeps.append({
                    "index": int(idx),
                    "direction": "bearish",
                    "level": smart_fmt(pivot["price"]),
                    "extreme": smart_fmt(float(row["high"])),
                })
                break
    for pivot in lows:
        later = working.iloc[pivot.get("confirmed_index", pivot["index"]) + 1:]
        for idx, row in later.iterrows():
            if float(row["low"]) < pivot["price"] and float(row["close"]) > pivot["price"]:
                sweeps.append({
                    "index": int(idx),
                    "direction": "bullish",
                    "level": smart_fmt(pivot["price"]),
                    "extreme": smart_fmt(float(row["low"])),
                })
                break

    sweeps = sorted(sweeps, key=lambda item: item["index"])
    for sweep in sweeps:
        sweep.update(_level_metadata(working, sweep['index'], sweep))
    all_gaps = _fair_value_gaps(working, limit=50, include_mitigated=True)
    active_fvg = [gap for gap in all_gaps if not gap.get("mitigated")][:limit]
    inverse_fvg = []
    for gap in all_gaps:
        if not gap.get("mitigated"):
            continue
        later = working.iloc[gap['formed_index'] + 1:]
        breaks = later[later['close'] < float(gap['low'])] if gap['bias'] == 'bullish' else later[later['close'] > float(gap['high'])]
        if breaks.empty:
            continue  # A wick filling a gap alone does not confirm an inverse FVG.
        flip_index = int(breaks.index[0])
        after_flip = working.iloc[flip_index + 1:]
        invalidated = ((after_flip['close'] > float(gap['high'])).any() if gap['bias'] == 'bullish'
                       else (after_flip['close'] < float(gap['low'])).any())
        if invalidated:
            continue
        flipped_bias = "bearish" if gap.get("bias") == "bullish" else "bullish"
        inverse_fvg.append({
            "bias": flipped_bias,
            "source_bias": gap.get("bias"),
            "low": gap.get("low"),
            "high": gap.get("high"),
            "consumed_pct": gap.get("consumed_pct", 100.0),
            **_level_metadata(working, flip_index, gap),
        })

    latest_sweep = sweeps[-1] if sweeps else {}
    return {
        "latest_sweep": {k: v for k, v in latest_sweep.items() if k != "index"},
        "sweeps": [{k: v for k, v in sweep.items() if k != "index"} for sweep in sweeps[-limit:]],
        "active_fvg": active_fvg[:limit],
        "inverse_fvg": sorted(inverse_fvg, key=lambda item: item['age_bars'])[:limit],
    }


def detect_rsi_divergence(close, rsi, lookback=20):
    """
    检测 RSI 与价格的顶/底背离（简化版）
    - 看涨背离：价格创近期新低但 RSI 未创新低（底部抬高）
    - 看跌背离：价格创近期新高但 RSI 未创新高（顶部降低）
    返回: "看涨背离 🟢" | "看跌背离 🔴" | None
    """
    if len(close) < lookback + 5 or len(rsi) < lookback + 5:
        return None

    recent_close = close.iloc[-lookback:]
    recent_rsi = rsi.iloc[-lookback:]
    prev_close = close.iloc[-(lookback * 2):-lookback]
    prev_rsi = rsi.iloc[-(lookback * 2):-lookback]

    if len(prev_close) < 10:
        return None

    curr_price_low = recent_close.min()
    prev_price_low = prev_close.min()
    curr_rsi_low = recent_rsi.min()
    prev_rsi_low = prev_rsi.min()

    # 看涨背离：价格创新低但 RSI 底部抬高
    if curr_price_low < prev_price_low and curr_rsi_low > prev_rsi_low * 1.03:
        return "看涨背离 🟢"

    curr_price_high = recent_close.max()
    prev_price_high = prev_close.max()
    curr_rsi_high = recent_rsi.max()
    prev_rsi_high = prev_rsi.max()

    # 看跌背离：价格创新高但 RSI 顶部降低
    if curr_price_high > prev_price_high and curr_rsi_high < prev_rsi_high * 0.97:
        return "看跌背离 🔴"

    return None
