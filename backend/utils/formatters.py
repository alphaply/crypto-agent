"""Formatting helpers for compact agent-readable text."""


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
        entry = float(position.get("entry_price", 0) or 0)
        pnl = float(position.get("unrealized_pnl", 0) or 0)
        pnl_sign = "+" if pnl >= 0 else ""
        lines.append(f"[{side}] {symbol} | Amt: {amount} | Entry: {entry} | PnL: {pnl_sign}{pnl:.3f}")

    return "\n".join(lines)


def format_orders_to_agent_friendly(orders):
    if not orders:
        return "(No Active Orders)"

    lines = []
    for order in orders:
        if not order:
            continue
        side = str(order.get("side") or "").upper()
        pos_side = str(order.get("pos_side") or "BOTH").upper()
        raw_type = str(order.get("type") or order.get("raw_type") or "").upper()
        price = order.get("price")
        amount = order.get("amount")
        order_id = order.get("id", "N/A")

        tp = float(order.get("tp", 0) or order.get("take_profit", 0) or 0)
        sl = float(order.get("sl", 0) or order.get("stop_loss", 0) or 0)

        extras = ""
        if tp > 0 or sl > 0:
            extras = f" | TP: {tp} | SL: {sl}"
        if raw_type:
            extras = f" | Type: {raw_type}{extras}"

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

    return "\n".join(lines)


def format_market_data_to_text(data: dict) -> str:
    """Format market data into a compact agent prompt payload."""

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
        for item in items[:2]:
            bias = item.get("bias", "N/A")
            output.append(f"{prefix}:{bias}[{item.get('low')}~{item.get('high')}]")
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
        return (
            f"{structure.get('scope', 'N/A')} {structure.get('bias', 'N/A')} "
            f"{structure.get('type', 'N/A')} @ {structure.get('level', 'N/A')}"
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
        f"- 24h Vol: {vol_24h} | Long/Short ratio: {ls_ratio} | Account L/S: {ls_accounts}",
    ]

    news_context = data.get("news_context") or {}
    if news_context:
        headlines = news_context.get("headlines") or []
        headline_text = "; ".join(str(item) for item in headlines[:3]) if headlines else "none"
        output.append(f"- News: {headline_text}")
    output.append("")

    indicators = data.get("technical_indicators") or {}
    tf_order = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"]
    available_tfs = [tf for tf in tf_order if tf in indicators]

    monthly = indicators.get("1M", {})
    weekly = indicators.get("1w", {})
    if monthly and weekly:
        monthly_trend = monthly.get("trend", {})
        weekly_trend = weekly.get("trend", {})
        output.append("[Macro Trend]")
        output.append(
            f"- 1M ADX={monthly_trend.get('adx', 0)} "
            f"DI+={monthly_trend.get('di_plus', 0)} DI-={monthly_trend.get('di_minus', 0)}"
        )
        output.append(
            f"- 1w ADX={weekly_trend.get('adx', 0)} "
            f"DI+={weekly_trend.get('di_plus', 0)} DI-={weekly_trend.get('di_minus', 0)}"
        )
        output.append("")

    if not available_tfs:
        output.append("[Technical Indicators]")
        output.append("- No timeframe data")
        return "\n".join(output).strip()

    for tf in available_tfs:
        timeframe_data = indicators[tf]
        trend = timeframe_data.get("trend", {})
        adx = trend.get("adx", 0)
        di_plus = trend.get("di_plus", 0)
        di_minus = trend.get("di_minus", 0)
        atr = timeframe_data.get("atr", 0)
        vol_stat = timeframe_data.get("volume_status") or timeframe_data.get("volume_analysis", {}).get("status", "N/A")
        output.append(f"[{tf}] ADX={adx} DI+={di_plus} DI-={di_minus} | ATR={atr} | Vol={vol_stat}")

        ema = timeframe_data.get("ema", {})
        ema_line = (
            f"- EMA: 20={ema.get('ema_20', 0)} / 50={ema.get('ema_50', 0)} / "
            f"100={ema.get('ema_100', 0)} / 200={ema.get('ema_200', 0)}"
        )
        if timeframe_data.get("vwap"):
            ema_line += f" | VWAP={timeframe_data.get('vwap')}"
        output.append(ema_line)

        rsi_data = timeframe_data.get("rsi_analysis", {})
        rsi_text = f"RSI={rsi_data.get('rsi', 0)}"
        if rsi_data.get("divergence"):
            rsi_text += f" [{clean_text(rsi_data.get('divergence'))}]"
        macd = timeframe_data.get("macd", {})
        output.append(
            f"- {rsi_text} | MACD: Diff={macd.get('diff', 0)} "
            f"Hist={macd.get('hist', 0)} ({clean_text(macd.get('momentum', ''))})"
        )

        bb = timeframe_data.get("bollinger", {})
        output.append(f"- BB: Up={bb.get('up', 0)} Low={bb.get('low', 0)} Width={bb.get('width', 0)}")

        closes = timeframe_data.get("recent_closes", [])
        opens = timeframe_data.get("recent_opens", [])
        highs = timeframe_data.get("recent_highs", [])
        lows = timeframe_data.get("recent_lows", [])
        if closes and len(closes) == len(opens) == len(highs) == len(lows):
            ohlc_list = [
                f"[{open_},{high},{low},{close}]"
                for open_, high, low, close in zip(opens[-10:], highs[-10:], lows[-10:], closes[-10:])
            ]
            output.append(f"- Recent {len(ohlc_list)} candles (O,H,L,C): {', '.join(ohlc_list)}")

        output.append(f"- {fmt_smc(timeframe_data.get('smc') or {})}")
        output.append(f"- {fmt_liquidity_sweep_ifvg(timeframe_data.get('liquidity_sweep_ifvg') or {})}")
        output.append("")

    return "\n".join(output).strip()


def format_market_data_to_markdown(data: dict) -> str:
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
