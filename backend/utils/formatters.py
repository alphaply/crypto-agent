"""
格式化工具函数模块
将复杂的数据结构转换为 Agent 易读的文本格式
"""

def escape_markdown_special_chars(text: str) -> str:
    """
    转义Markdown中的特殊字符，避免被错误解析
    特别处理波浪号，防止被当作删除线标记
    """
    if not text:
        return ""
    # 直接将所有波浪号替换为 \~，这是 Markdown 的转义方式
    # 这样既不会被解析为删除线，在渲染时也会显示为原生的 ~
    return text.replace('~', r'\~')


def format_positions_to_agent_friendly(positions: list) -> str:
    if not positions:
        return "无持仓 (No Positions)"
    
    lines = []
    for p in positions:
        side = p.get('side', '').upper()
        sym = p.get('symbol', '').split(':')[0]
        amt = float(p.get('amount', 0))
        entry = float(p.get('entry_price', 0))
        pnl = float(p.get('unrealized_pnl', 0))
        pnl_sign = "+" if pnl >= 0 else ""
        lines.append(f"[{side}] {sym} | Amt: {amt} | Entry: {entry} | PnL: {pnl_sign}{pnl:.3f}")
        
    return "\n".join(lines)
def format_orders_to_agent_friendly(orders):
    if not orders:
        return "(无挂单)"
    
    lines = []
    for o in orders:
        if not o:
            continue
        side = (o.get('side') or '').upper()
        pos_side = (o.get('pos_side') or 'BOTH').upper()
        raw_type = (o.get('type') or o.get('raw_type') or '').upper()
        price = o.get('price')
        amt = o.get('amount')
        oid = o.get('id', 'N/A')
        
        tp = float(o.get('tp', 0) or o.get('take_profit', 0))
        sl = float(o.get('sl', 0) or o.get('stop_loss', 0))
        
        extras = ""
        if tp > 0 or sl > 0:
            extras = f" | TP: {tp} | SL: {sl}"
        if raw_type:
            extras = f" | Type: {raw_type}{extras}"
        action_str = side
        if pos_side == 'LONG':
            if side == 'BUY': action_str = "OPEN LONG (加多)"
            if side == 'SELL': action_str = "CLOSE LONG (平多/止盈损)"
        elif pos_side == 'SHORT':
            if side == 'SELL': action_str = "OPEN SHORT (加空)"
            if side == 'BUY': action_str = "CLOSE SHORT (平空/止盈损)"
        
        lines.append(f"ID:'{oid}'- [{action_str}] 数量: {amt} @ 价格: {price} {extras}")
        
    return "\n".join(lines)

# def format_orders_to_agent_friendly(orders: list) -> str:
#     if not orders:
#         return "无活跃挂单 (No Active Orders)"

#     lines = []
#     for o in orders:
#         side = o.get('side', '').upper()
#         raw_type = str(o.get('type', 'LIMIT'))
#         order_type = 'LIMIT' if 'limit' in raw_type.lower() or '限价' in raw_type else raw_type.upper()

#         oid = o.get('id', 'N/A')
#         price = float(o.get('price', 0))
#         amt = float(o.get('amount', 0))

#         tp = float(o.get('tp', 0) or o.get('take_profit', 0))
#         sl = float(o.get('sl', 0) or o.get('stop_loss', 0))
        
#         extras = ""
#         if tp > 0 or sl > 0:
#             extras = f" | TP: {tp} | SL: {sl}"
        
#         lines.append(f"[{side}] {order_type} | ID: '{oid}' | Price: {price} | Amt: {amt}{extras}")

#     return "\n".join(lines)


def format_market_data_to_text(data: dict) -> str:
    """Format market data into a compact, cleaner prompt payload."""
    def fmt_num(num):
        try:
            num = float(num or 0)
        except Exception:
            num = 0
        if num > 1_000_000_000: return f"{num/1_000_000_000:.1f}B"
        if num > 1_000_000: return f"{num/1_000_000:.1f}M"
        if num > 1_000: return f"{num/1_000:.1f}K"
        return f"{num:.0f}"

    def normalize_trend(status):
        text = str(status or "N/A")
        mapping = {
            "Strong Uptrend": "上涨排列",
            "Strong Downtrend": "下跌排列",
            "Bullish Neutral": "震荡偏多",
            "Bearish Neutral": "震荡偏空",
            "Consolidation": "区间震荡",
        }
        return mapping.get(text, text)

    def clean_text(value):
        text = str(value or "").replace("⚠️", "").replace("🔴", "").replace("🟢", "").strip()
        momentum_map = {
            "多头加速": "hist扩大",
            "空头加速": "hist扩大",
            "多头减速": "hist收敛",
            "空头减速": "hist收敛",
            "零轴附近": "hist持平",
        }
        return momentum_map.get(text, text)

    def fmt_ob(blocks):
        if not blocks:
            return "无"
        items = []
        for block in blocks[:2]:
            bias = "多头OB" if block.get("bias") == "bullish" else "空头OB"
            items.append(f"{bias}[{block.get('low')}~{block.get('high')}]")
        return ", ".join(items)

    def fmt_fvg(gaps):
        if not gaps:
            return "无"
        items = []
        for gap in gaps[:2]:
            bias = "多头FVG" if gap.get("bias") == "bullish" else "空头FVG"
            items.append(f"{bias}[{gap.get('low')}~{gap.get('high')}]")
        return ", ".join(items)

    def fmt_liquidity(liq):
        if not liq:
            return "无"
        parts = [f"SwingH={liq.get('swing_high')}", f"SwingL={liq.get('swing_low')}"]
        if liq.get("eqh"):
            parts.append(f"EQH={','.join(str(x) for x in liq.get('eqh', [])[:2])}")
        if liq.get("eql"):
            parts.append(f"EQL={','.join(str(x) for x in liq.get('eql', [])[:2])}")
        return " ".join(parts)

    def fmt_smc(smc):
        if not smc:
            return "SMC: 无可用结构"
        structure = smc.get("structure") or {}
        if structure:
            bias = "多头" if structure.get("bias") == "bullish" else "空头"
            structure_text = f"{bias} {structure.get('type', 'N/A')} @ {structure.get('level', 'N/A')}"
        else:
            structure_text = "暂无BOS/CHoCH"
        zone = smc.get("zone") or {}
        return (
            f"SMC: Structure={structure_text} | "
            f"OB={fmt_ob(smc.get('order_blocks') or [])} | "
            f"FVG={fmt_fvg(smc.get('fvg') or [])} | "
            f"Liquidity={fmt_liquidity(smc.get('liquidity') or {})} | "
            f"Zone={zone.get('name', 'N/A')}[{zone.get('low', 'N/A')}~{zone.get('high', 'N/A')}]"
        )

    current_price = data.get("current_price", 0)
    atr_base = data.get("atr_base", 0)
    sent = data.get("sentiment") or {}
    funding = float(sent.get("funding_rate", 0) or 0) * 100
    oi = fmt_num(sent.get("open_interest", 0))
    vol_24h = fmt_num(sent.get("24h_quote_vol", 0))
    ls_ratio = sent.get("ls_ratio", "N/A")
    ls_accounts = sent.get("ls_accounts", "N/A")

    output = [
        "【市场快照】",
        f"• 价格: {current_price} | 基准ATR: {atr_base} | 资金费率: {funding:.4f}% | OI: {oi}",
        f"• 24h量: {vol_24h} | 大户多空比: {ls_ratio} | 人数多空比: {ls_accounts}",
    ]
    news_context = data.get("news_context") or {}
    if news_context:
        risk = news_context.get("risk_level", "normal")
        headlines = news_context.get("headlines") or []
        headline_text = "；".join(str(item) for item in headlines[:3]) if headlines else "无重大消息"
        output.append(f"• 消息面风险: {risk} | {headline_text}")
    output.append("")

    indicators = data.get("technical_indicators") or {}
    tf_order = ['1m', '5m', '15m', '30m', '1h', '4h', '1d', '1w', '1M']
    available_tfs = [tf for tf in tf_order if tf in indicators]

    monthly = indicators.get('1M', {})
    weekly = indicators.get('1w', {})
    if monthly and weekly:
        monthly_trend = monthly.get('trend', {})
        weekly_trend = weekly.get('trend', {})
        monthly_status = normalize_trend(monthly_trend.get('status', 'N/A'))
        weekly_status = normalize_trend(weekly_trend.get('status', 'N/A'))
        monthly_adx = monthly_trend.get('adx', 0)
        weekly_adx = weekly_trend.get('adx', 0)
        if monthly_status == weekly_status:
            macro_label = f"月/周方向一致（{monthly_status}），顺该方向的计划优先。"
        else:
            macro_label = "月/周方向不一致，降低信心，等待低周期结构确认。"
        output.append("[Macro Trend]")
        output.append(f"- 1M={monthly_status} ADX={monthly_adx} | 1w={weekly_status} ADX={weekly_adx}")
        output.append(f"- {macro_label}")
        output.append("")

    if not available_tfs:
        output.append("【技术指标】")
        output.append("• 暂无可用周期数据")
        return "\n".join(output).strip()

    for tf in available_tfs:
        d = indicators[tf]
        trend = d.get('trend', {})
        t_status = normalize_trend(trend.get('status', 'N/A'))
        t_strength = trend.get('strength', 'N/A')
        adx = trend.get('adx', 0)
        di_plus = trend.get('di_plus', 0)
        di_minus = trend.get('di_minus', 0)
        atr = d.get('atr', 0)
        vol_stat = d.get('volume_status') or d.get('volume_analysis', {}).get('status', 'N/A')
        output.append(f"【{tf}周期】{t_status} | ADX={adx} ({t_strength}) DI+={di_plus} DI-={di_minus} | ATR={atr} | Vol={vol_stat}")

        ema = d.get('ema', {})
        ema_line = f"• EMA: 20={ema.get('ema_20', 0)} / 50={ema.get('ema_50', 0)} / 200={ema.get('ema_200', 0)}"
        if d.get('vwap'):
            ema_line += f" | VWAP={d.get('vwap')}"
        output.append(ema_line)

        rsi_data = d.get('rsi_analysis', {})
        rsi_str = f"RSI={rsi_data.get('rsi', 0)}"
        if rsi_data.get('divergence'):
            rsi_str += f" [{clean_text(rsi_data.get('divergence'))}]"
        macd = d.get('macd', {})
        output.append(f"• {rsi_str} | MACD: Diff={macd.get('diff', 0)} Hist={macd.get('hist', 0)} ({clean_text(macd.get('momentum', ''))})")

        bb = d.get('bollinger', {})
        output.append(f"• BB: Up={bb.get('up', 0)} Low={bb.get('low', 0)} Width={bb.get('width', 0)}")

        closes = d.get('recent_closes', [])
        opens = d.get('recent_opens', [])
        highs = d.get('recent_highs', [])
        lows = d.get('recent_lows', [])
        if closes and len(closes) == len(opens) == len(highs) == len(lows):
            ohlc_list = [f"[{o},{h},{l},{c}]" for o, h, l, c in zip(opens[-5:], highs[-5:], lows[-5:], closes[-5:])]
            output.append(f"• 近{len(ohlc_list)}根K线(O,H,L,C): {', '.join(ohlc_list)}")

        output.append(f"• {fmt_smc(d.get('smc') or {})}")
        output.append("")

    return "\n".join(output).strip()


def format_market_data_to_markdown(data: dict) -> str:
    """
    将复杂的市场 JSON 数据转换为 Markdown 表格（精简版 v2）
    """
    def fmt_num(num):
        if num > 1_000_000_000: return f"{num/1_000_000_000:.1f}B"
        if num > 1_000_000: return f"{num/1_000_000:.1f}M"
        if num > 1_000: return f"{num/1_000:.1f}K"
        return f"{num:.0f}"

    current_price = data.get("current_price", 0)
    atr_base = data.get("atr_base", 0)
    sent = data.get("sentiment", {})
    funding = sent.get("funding_rate", 0) * 100 
    
    header = (
        f"**Snapshot** | Price: {current_price} | Base ATR: {atr_base}\n"
        f"Sentiment: Fund: {funding:.4f}% | Vol24h: {fmt_num(sent.get('24h_quote_vol', 0))}\n"
    )

    table_header = (
        "| TF | Trend | RSI | MACD (Hist) | Momentum | BB Width | EMA (20/50/200) | POC | HVN |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
    )
    
    rows = []
    indicators = data.get("technical_indicators", {})
    tf_order = ['1m', '5m', '15m', '30m', '1h', '4h', '1d', '1w', '1M']
    available_tfs = [tf for tf in tf_order if tf in indicators]
    
    for tf in available_tfs:
        d = indicators[tf]

        trend = d.get('trend', {}).get('status', 'N/A')

        rsi_data = d.get('rsi_analysis', {})
        rsi = rsi_data.get('rsi', 0)
        divergence = rsi_data.get('divergence', '')
        rsi_str = f"{rsi:.1f}"
        if divergence:
            rsi_str += f" {divergence}"

        macd = d.get('macd', {})
        hist = macd.get('hist', 0)
        momentum = macd.get('momentum', '')

        bb = d.get('bollinger', {})
        width = bb.get('width', 0)

        ema = d.get('ema', {})
        e_str = f"{ema.get('ema_20')}/{ema.get('ema_50')}/{ema.get('ema_200')}"

        vp = d.get('vp', {})
        poc = vp.get('poc', 0)
        hvns = vp.get('hvns', [])[:3]
        h_str = ",".join([str(x) for x in hvns])

        row = f"| {tf} | {trend} | {rsi_str} | {hist} | {momentum} | {width} | {e_str} | {poc} | {h_str} |"
        rows.append(row)

    return header + table_header + "\n".join(rows)
