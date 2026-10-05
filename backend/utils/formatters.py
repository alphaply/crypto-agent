"""Formatting helpers for compact agent-readable text."""

import math


def escape_markdown_special_chars(text: str) -> str:
    if not text:
        return ""
    return text.replace("~", r"\~")


def format_positions_to_agent_friendly(positions: list) -> str:
    if not positions:
        return "No Positions"

    lines = []
    for position in positions:
        side = str(position.get("side", "")).upper()
        symbol = str(position.get("symbol", "")).split(":")[0]
        amount = float(position.get("amount", 0) or 0)
        if position.get('market_type') == 'spot':
            lines.append(f"[SPOT] {symbol} | Account holdings: {amount} | Free: {position.get('available_amount', 0)} | Cost basis/PnL: N/A (account balance, not task attribution)")
            continue
        entry = float(position.get("entry_price", 0) or 0)
        pnl = float(position.get("unrealized_pnl", 0) or 0)
        pnl_sign = "+" if pnl >= 0 else ""
        lines.append(f"[{side}] {symbol} | Amt: {amount} | Entry: {entry} | PnL: {pnl_sign}{pnl:.3f}")

    return "\n".join(lines)


def format_orders_to_agent_friendly(orders, protection_plans=None, symbol=""):
    orders = [order for order in (orders or []) if order]
    lines = []
    for order in orders:
        if not order:
            continue
        side = str(order.get("side") or "").upper()
        pos_side = str(order.get("pos_side") or "BOTH").upper()
        raw_type = str(order.get("type") or order.get("raw_type") or "").upper()
        price = order.get("price")
        amount = order.get("amount")
        order_id = order.get("id") or order.get("order_id") or "N/A"

        tp = float(order.get("tp", 0) or order.get("take_profit", 0) or 0)
        sl = float(order.get("sl", 0) or order.get("stop_loss", 0) or 0)

        extras = ""
        if tp > 0 or sl > 0:
            extras = f" | TP: {tp} | SL: {sl}"
        if raw_type:
            extras = f" | Type: {raw_type}{extras}"
        if order.get('symbol'):
            extras = f" | Symbol: {order['symbol']}{extras}"

        action = side
        if pos_side == "LONG":
            if side == "BUY":
                action = "OPEN LONG"
            if side == "SELL":
                action = "CLOSE LONG"
        elif pos_side == "SHORT":
            if side == "SELL":
                action = "OPEN SHORT"
            if side == "BUY":
                action = "CLOSE SHORT"

        lines.append(f"ID:'{order_id}' - [{action}] Amount: {amount} @ Price: {price}{extras}")

    if protection_plans is not None:
        from backend.utils.order_context import merge_order_protection

        return merge_order_protection(orders, lines, protection_plans, symbol)
    return "\n".join(lines) or "(No Active Orders)"


def _is_spot_market_data(data: dict) -> bool:
    return data.get('indicator_profile') == 'spot_long_term' or any(
        item.get('indicator_profile') == 'spot_long_term'
        for item in (data.get('technical_indicators') or {}).values()
    )


def _format_spot_market_data(data: dict) -> str:
    def fmt(value):
        return 'N/A' if value is None else str(value)

    output = [
        f"[Spot market: {data.get('symbol') or ''}]",
        f"- Live/reference price: {fmt(data.get('current_price'))}",
        '- 4h: execution context; 1d: trend and accumulation; 1w: long-term regime.',
        '- Indicators use closed candles only. N/A means insufficient/unavailable data, not zero.',
        '- Drawdown is signed (close/high - 1); RVOL20 uses the previous 20 closed candles. These describe history, not buy instructions.',
    ]
    indicators = data.get('technical_indicators') or {}
    for tf in (list(indicators) or ['4h', '1d', '1w']):
        if tf not in indicators:
            output.append(f'[{tf}] N/A (no closed-candle data)')
            continue
        item = indicators[tf]
        ema, spot = item.get('ema') or {}, item.get('spot_context') or {}
        quality = item.get('data_quality') or {}
        output.extend([
            f"[{tf}] Close={fmt(item.get('price'))} | EMA20/50/200={fmt(ema.get('ema_20'))}/{fmt(ema.get('ema_50'))}/{fmt(ema.get('ema_200'))}",
            f"- RSI14={fmt((item.get('rsi_analysis') or {}).get('rsi'))} | ATR14={fmt(item.get('atr'))} ({fmt(spot.get('atr_pct'))}%) | RVOL20={fmt((item.get('volume_analysis') or {}).get('ratio'))}x",
            f"- High({fmt(spot.get('high_lookback_bars'))} bars)={fmt(spot.get('recent_high'))} | Drawdown={fmt(spot.get('drawdown_from_high_pct'))}% | Available high-history bars={fmt(spot.get('high_available_bars'))}",
            f"- Close vs EMA200={fmt(spot.get('price_vs_ema200_pct'))}% | Return20bars={fmt(spot.get('return_20_bars_pct'))}%",
            f"- Data: {quality.get('basis', 'unknown')} | last close={quality.get('last_closed_at')} | bars={quality.get('bars')} | excluded forming={quality.get('forming_candles_excluded')}",
        ])
        if quality.get('stale') or quality.get('gap_count') or quality.get('invalid_candles_excluded'):
            output.append(f"- DATA QUALITY WARNING: stale={quality.get('stale')}, gaps={quality.get('gap_count', 0)}, invalid bars={quality.get('invalid_candles_excluded', 0)}; verify before submitting orders.")
        if quality.get('ema_warmup_bars'):
            output.append(f"- EMA warm-up warning (<3x span): {quality['ema_warmup_bars']}")
        times, closes = item.get('recent_times') or [], item.get('recent_closes') or []
        if len(times) == len(closes) and closes:
            output.append('- Recent closed prices (UTC): ' + ', '.join(f'{ts}={fmt(close)}' for ts, close in zip(times, closes)))
    news = data.get('news_context') or {}
    if news.get('digest'):
        output.append(f"- News context{' [cached/stale]' if news.get('stale') else ''}: {news['digest']}")
    return '\n'.join(output)


def format_market_data_to_text(data: dict) -> str:
    if _is_spot_market_data(data):
        return _format_spot_market_data(data)
    """Format market data into a compact agent prompt payload."""

    def fmt_value(value):
        if value is None:
            return "N/A"
        if isinstance(value, (int, float)) and not math.isfinite(value):
            return "N/A"
        return str(value)

    def fmt_num(num):
        try:
            num = float(num or 0)
        except Exception:
            num = 0
        if num > 1_000_000_000:
            return f"{num / 1_000_000_000:.1f}B"
        if num > 1_000_000:
            return f"{num / 1_000_000:.1f}M"
        if num > 1_000:
            return f"{num / 1_000:.1f}K"
        return f"{num:.0f}"

    def clean_text(value):
        return str(value or "").replace("鈿狅笍", "").replace("馃敶", "").replace("馃煝", "").strip()

    def fmt_zone_items(items, prefix):
        if not items:
            return "none"
        output = []
        nearby = [item for item in items if item.get('distance_atr') is None or item['distance_atr'] <= 6]
        if not nearby:
            return "none within 6 ATR (historical zones omitted)"
        for item in sorted(nearby, key=lambda x: x.get('distance_atr') or 0)[:2]:
            bias = item.get("bias", "N/A")
            suffix = f" age={item['age_bars']} bars dist={item.get('distance_atr')}ATR" if 'age_bars' in item else ''
            output.append(f"{prefix}:{bias}[{item.get('low')}~{item.get('high')}]{suffix}")
        return ", ".join(output)

    def fmt_liquidity(liquidity):
        if not liquidity:
            return "none"
        parts = [f"SwingH={liquidity.get('swing_high')}", f"SwingL={liquidity.get('swing_low')}"]
        if liquidity.get("eqh"):
            parts.append(f"EQH={','.join(str(x) for x in liquidity.get('eqh', [])[:2])}")
        if liquidity.get("eql"):
            parts.append(f"EQL={','.join(str(x) for x in liquidity.get('eql', [])[:2])}")
        return " ".join(parts)

    def fmt_structure(structure):
        if not structure:
            return "none"
        suffix = f" age={structure['age_bars']} bars dist={structure.get('distance_atr')}ATR" if 'age_bars' in structure else ''
        return (
            f"{structure.get('scope', 'N/A')} {structure.get('bias', 'N/A')} "
            f"{structure.get('type', 'N/A')} @ {structure.get('level', 'N/A')}{suffix}"
        )

    def fmt_smc(smc):
        if not smc:
            return "SMC: none"
        zone = smc.get("zone") or {}
        return (
            f"SMC: Structure={fmt_structure(smc.get('structure') or {})} | "
            f"Internal={fmt_structure(smc.get('internal_structure') or {})} | "
            f"Swing={fmt_structure(smc.get('swing_structure') or {})} | "
            f"OB={fmt_zone_items(smc.get('order_blocks') or [], 'OB')} | "
            f"FVG={fmt_zone_items(smc.get('fvg') or [], 'FVG')} | "
            f"Liquidity={fmt_liquidity(smc.get('liquidity') or {})} | "
            f"Zone={zone.get('name', 'N/A')}[{zone.get('low', 'N/A')}~{zone.get('high', 'N/A')}]"
        )

    def fmt_liquidity_sweep_ifvg(payload):
        if not payload:
            return "Liquidity/IFVG: none"
        latest = payload.get("latest_sweep") or {}
        sweep_text = "none"
        if latest:
            sweep_text = f"{latest.get('direction', 'N/A')} sweep @ {latest.get('level', 'N/A')}"
            if 'age_bars' in latest:
                sweep_text += f" age={latest['age_bars']} bars"
        ifvg = fmt_zone_items(payload.get("inverse_fvg") or [], "IFVG")
        fvg = fmt_zone_items(payload.get("active_fvg") or [], "FVG")
        return f"Liquidity/IFVG: Sweep={sweep_text} | IFVG={ifvg} | FVG={fvg}"

    current_price = data.get("current_price", 0)
    atr_base = data.get("atr_base", 0)
    sentiment = data.get("sentiment") or {}
    funding = float(sentiment.get("funding_rate", 0) or 0) * 100
    oi = fmt_num(sentiment.get("open_interest", 0))
    vol_24h = fmt_num(sentiment.get("24h_quote_vol", 0))
    ls_ratio = sentiment.get("ls_ratio", "N/A")
    ls_accounts = sentiment.get("ls_accounts", "N/A")

    output = [
        "[Market Snapshot]",
        f"- Price: {current_price} | Base ATR: {atr_base} | Funding: {funding:.4f}% | OI: {oi}",
        f"- 24h quote Vol: {vol_24h} | Binance top-position L/S (5m): {ls_ratio} | Global account L/S (5m): {ls_accounts}",
        "- OI is a snapshot (exchange quantity units); without changes/contract size it is not a directional signal.",
    ]

    ratio_labels = {'ls_accounts': 'Global account L/S', 'ls_ratio': 'Top trader position L/S',
                    'top_ls_accounts': 'Top trader account L/S', 'taker_buy_sell_ratio': 'Taker buy/sell volume'}
    for key, detail in (sentiment.get('ratio_details') or {}).items():
        label = ratio_labels.get(key, key)
        if not detail.get('available'):
            output.append(f"- Binance {label} (5m): N/A ({'stale >15m' if detail.get('stale') else detail.get('reason', 'unavailable')})")
            continue
        change = detail.get('change_5m_pct')
        change_text = f'{change:+.2f}%' if change is not None else 'N/A'
        output.append(f"- Binance {label} (5m): {detail['value']:.4f} | 5m relative change: {change_text} | timestamp_ms: {detail['timestamp_ms']} | age: {detail['age_seconds']}s")
    output.append('- L/S account ratios measure account counts, top-position ratios measure top-trader positioning, and taker ratios measure aggressive volume. They are different populations, not win probabilities or standalone entry/reversal signals; N/A is not neutral.')

    news_context = data.get("news_context") or {}
    if news_context:
        digest = str(news_context.get("digest") or "").strip()
        headlines = news_context.get("headlines") or []
        headline_text = "; ".join(str(item) for item in headlines[:10]) if headlines else ("none selected" if news_context.get("available") else "unavailable")
        stale_text = " [cached/stale]" if news_context.get("stale") else ""
        output.append(f"- News context{stale_text}: {headline_text}")
        predictions = [item['title'] for item in news_context.get('items', []) if item.get('category') == 'prediction_market']
        if predictions:
            output.append('- Prediction markets (market prices, not facts): ' + '; '.join(predictions))
        if digest:
            output.append(f"- Compressed news intelligence:\n{digest}")
    output.append("")

    indicators = data.get("technical_indicators") or {}
    tf_order = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"]
    available_tfs = [tf for tf in tf_order if tf in indicators]

    if not available_tfs:
        output.append("[Technical Indicators]")
        output.append("- No timeframe data")
        return "\n".join(output).strip()

    output.append("[Technical context: closed candles, oldest to newest; price is the last closed-bar reference, not a live quote. N/A means unavailable.]")
    output.append("V uses exchange OHLCV volume units. RVOL20 compares the last candle with the previous 20 closed candles; ATR-normalized distances are signed. Indicators describe history, not independent probabilities.")
    output.append("CHOP: <38.2 trending, >61.8 choppy (descriptive, no direction); CMF is an OHLCV pressure proxy, not net capital flow. Squeeze on=BB inside KC, off=both bands outside, neutral=mixed/touching; release_age=bars since observed on→off (0=this bar, N/A=not observed/reset). Durations are bounded by available history; release alone gives no direction.")
    for tf in available_tfs:
        timeframe_data = indicators[tf]
        trend = timeframe_data.get("trend") or {}
        adx = fmt_value(trend.get("adx"))
        di_plus = fmt_value(trend.get("di_plus"))
        di_minus = fmt_value(trend.get("di_minus"))
        atr = fmt_value(timeframe_data.get("atr"))
        context = timeframe_data.get('decision_context') or {}
        trend_context = context.get('trend') or {}
        momentum = context.get('momentum') or {}
        volatility = context.get('volatility') or {}
        regime = context.get('regime') or {}
        volume = timeframe_data.get("volume_analysis") or {}
        output.append(
            f"[{tf}] ADX={adx} DI+={di_plus} DI-={di_minus} | Close={fmt_value(timeframe_data.get('price'))} "
            f"| ATR={atr} ({fmt_value(volatility.get('atr_pct'))}%) | RVOL20={fmt_value(volume.get('ratio'))}x"
        )
        quality = timeframe_data.get('data_quality') or {}
        if quality.get('stale') or quality.get('gap_count') or quality.get('invalid_candles_excluded'):
            output.append(f"- DATA QUALITY WARNING: stale={quality.get('stale')}, gaps={quality.get('gap_count', 0)}, invalid bars={quality.get('invalid_candles_excluded', 0)}. Refresh/verify data before considering new entries; indicators may be unreliable.")
        if quality:
            output.append(f"- Data: {quality.get('basis')} | last close={quality.get('last_closed_at')} | age={fmt_value(quality.get('age_seconds'))}s | calculation bars={fmt_value(quality.get('bars'))} | excluded forming={quality.get('forming_candles_excluded')}")
            if quality.get('ema_warmup_bars'):
                output.append(f"- EMA warm-up warning (<3x span): {quality['ema_warmup_bars']}")

        ema = timeframe_data.get("ema") or {}
        ema_line = (
            f"- EMA: 20={fmt_value(ema.get('ema_20'))} / 50={fmt_value(ema.get('ema_50'))} / "
            f"100={fmt_value(ema.get('ema_100'))} / 200={fmt_value(ema.get('ema_200'))}"
        )
        if trend_context:
            ema_line += (
                f" | (C-EMA20)/ATR={fmt_value(trend_context.get('price_minus_ema20_atr'))}"
                f" | (EMA20-EMA50)/ATR={fmt_value(trend_context.get('ema20_minus_ema50_atr'))}"
                f" | EMA20 Δ3bars={fmt_value(trend_context.get('ema20_change_3_bars_pct'))}%"
            )
        if timeframe_data.get("vwap"):
            ema_line += f" | VWAP={timeframe_data.get('vwap')} anchor={timeframe_data.get('vwap_anchor') or 'unknown'}"
        output.append(ema_line)

        rsi_data = timeframe_data.get("rsi_analysis") or {}
        rsi_text = f"RSI={fmt_value(rsi_data.get('rsi'))}"
        if rsi_data.get("divergence"):
            rsi_text += f" [{clean_text(rsi_data.get('divergence'))}]"
        macd = timeframe_data.get("macd") or {}
        momentum_line = f"- {rsi_text} | MACD: Diff={fmt_value(macd.get('diff'))} Hist={fmt_value(macd.get('hist'))} ({clean_text(macd.get('momentum', ''))})"
        if momentum:
            momentum_line += (
                f" | RSI Δ3bars={fmt_value(momentum.get('rsi_change_3_bars'))}"
                f" | Hist/ATR={fmt_value(momentum.get('macd_hist_atr'))}"
                f" | ΔHist1bar/ATR={fmt_value(momentum.get('macd_hist_change_1_bar_atr'))}"
            )
        output.append(momentum_line)

        bb = timeframe_data.get("bollinger") or {}
        output.append(f"- BB(20,2σ,population): Up={fmt_value(bb.get('up'))} Low={fmt_value(bb.get('low'))} Width={fmt_value(bb.get('width'))} %B={fmt_value(bb.get('percent_b'))} width percentile(≤120)={fmt_value(bb.get('width_percentile_120'))} | last range/ATR={fmt_value(volatility.get('last_range_atr'))}")
        if regime:
            chop, cmf, squeeze = (regime.get(key) or {} for key in ('chop', 'cmf', 'squeeze'))
            output.append(
                f"- CHOP14={fmt_value(chop.get('value'))} Δ3={fmt_value(chop.get('change_3_bars'))}"
                f" {chop.get('state', 'unavailable')}({fmt_value(chop.get('state_bars'))} bars)"
                f" | CMF20={fmt_value(cmf.get('value'))} Δ3={fmt_value(cmf.get('change_3_bars'))}"
                f" {cmf.get('sign', 'unavailable')}({fmt_value(cmf.get('sign_bars'))} bars)"
                f" | Squeeze(BB20,2σ/KC20,1.5×SMA-TR)={squeeze.get('state', 'unavailable')}"
                f" on_bars={fmt_value(squeeze.get('on_bars'))} release_age={fmt_value(squeeze.get('bars_since_release'))}"
            )

        if momentum:
            output.append(f"- Close returns 1/3/15 bars (%): {fmt_value(momentum.get('return_1_bar_pct'))}/{fmt_value(momentum.get('return_3_bars_pct'))}/{fmt_value(momentum.get('return_15_bars_pct'))}")
        prior_range = context.get('prior_range_20') or {}
        if prior_range:
            output.append(f"- Previous 20-bar range (excluding last candle): {prior_range['low']}~{prior_range['high']} | (H-C)/ATR={fmt_value(prior_range.get('to_high_atr'))} (C-L)/ATR={fmt_value(prior_range.get('to_low_atr'))}; negative means crossed edge")

        vp = timeframe_data.get("vp") or {}
        if vp.get('available') is False:
            output.append(f"- Volume Profile: N/A ({vp.get('reason', 'unavailable')})")
        elif vp:
            hvns = ", ".join(str(value) for value in (vp.get("hvns") or [])[:3]) or "none"
            output.append(
                f"- Volume Profile: POC={vp.get('poc', 0)} VAH={vp.get('vah', 0)} "
                f"VAL={vp.get('val', 0)} HVN={hvns}"
            )
            if vp.get('window_bars'):
                output.append(f"  Window={vp['window_bars']} bars from {vp.get('window_start')} UTC; {vp.get('method')}")

        closes = timeframe_data.get("recent_closes", [])
        opens = timeframe_data.get("recent_opens", [])
        highs = timeframe_data.get("recent_highs", [])
        lows = timeframe_data.get("recent_lows", [])
        if closes and len(closes) == len(opens) == len(highs) == len(lows):
            times = timeframe_data.get('recent_times') or []
            volumes = timeframe_data.get('recent_volumes') or []
            has_time_volume = len(times) == len(volumes) == len(closes)
            candle_fields = 'UTC open,O,H,L,C,V' if has_time_volume else 'O,H,L,C'
            ohlc_list = [
                '[' + ','.join(fmt_value(value) for value in (
                    (times[i], opens[i], highs[i], lows[i], closes[i], volumes[i]) if has_time_volume
                    else (opens[i], highs[i], lows[i], closes[i])
                )) + ']'
                for i in range(max(0, len(closes) - 10), len(closes))
            ]
            output.append(f"- Recent {len(ohlc_list)} candles ({candle_fields}): {', '.join(ohlc_list)}")

        output.append(f"- {fmt_smc(timeframe_data.get('smc') or {})}")
        output.append(f"- {fmt_liquidity_sweep_ifvg(timeframe_data.get('liquidity_sweep_ifvg') or {})}")
        output.append("")

    return "\n".join(output).strip()


def format_market_data_to_markdown(data: dict) -> str:
    if _is_spot_market_data(data):
        return _format_spot_market_data(data)
    def fmt_num(num):
        if num > 1_000_000_000:
            return f"{num / 1_000_000_000:.1f}B"
        if num > 1_000_000:
            return f"{num / 1_000_000:.1f}M"
        if num > 1_000:
            return f"{num / 1_000:.1f}K"
        return f"{num:.0f}"

    current_price = data.get("current_price", 0)
    atr_base = data.get("atr_base", 0)
    sentiment = data.get("sentiment", {})
    funding = sentiment.get("funding_rate", 0) * 100

    header = (
        f"**Snapshot** | Price: {current_price} | Base ATR: {atr_base}\n"
        f"Sentiment: Fund: {funding:.4f}% | Vol24h: {fmt_num(sentiment.get('24h_quote_vol', 0))}\n"
    )

    table_header = (
        "| TF | ADX | RSI | MACD (Hist) | Momentum | BB Width | EMA (20/50/100/200) | POC | HVN |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
    )

    rows = []
    indicators = data.get("technical_indicators", {})
    tf_order = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"]
    available_tfs = [tf for tf in tf_order if tf in indicators]

    for tf in available_tfs:
        timeframe_data = indicators[tf]
        trend = timeframe_data.get("trend", {})
        adx = trend.get("adx", 0)
        rsi_data = timeframe_data.get("rsi_analysis", {})
        rsi = float(rsi_data.get("rsi", 0) or 0)
        rsi_text = f"{rsi:.1f}"
        if rsi_data.get("divergence"):
            rsi_text += f" {rsi_data.get('divergence')}"

        macd = timeframe_data.get("macd", {})
        hist = macd.get("hist", 0)
        momentum = macd.get("momentum", "")
        bb = timeframe_data.get("bollinger", {})
        width = bb.get("width", 0)
        ema = timeframe_data.get("ema", {})
        ema_text = f"{ema.get('ema_20')}/{ema.get('ema_50')}/{ema.get('ema_100')}/{ema.get('ema_200')}"
        vp = timeframe_data.get("vp", {})
        hvns = vp.get("hvns", [])[:3]
        hvn_text = ",".join([str(item) for item in hvns])
        rows.append(f"| {tf} | {adx} | {rsi_text} | {hist} | {momentum} | {width} | {ema_text} | {vp.get('poc', 0)} | {hvn_text} |")

    return header + table_header + "\n".join(rows)
