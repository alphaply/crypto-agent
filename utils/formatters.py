"""
格式化工具函数模块
将复杂的数据结构转换为 Agent 易读的文本格式
"""

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
        side = o.get('side', '').upper()
        pos_side = o.get('pos_side', 'BOTH').upper()
        price = o.get('price')
        amt = o.get('amount')
        
        # === 核心逻辑：转译为人类/AI 可读的意图 ===
        action_str = side
        if pos_side == 'LONG':
            if side == 'BUY': action_str = "OPEN LONG (多)"
            if side == 'SELL': action_str = "CLOSE LONG (平多/止盈损)"
        elif pos_side == 'SHORT':
            if side == 'SELL': action_str = "OPEN SHORT (空)"
            if side == 'BUY': action_str = "CLOSE SHORT (平空/止盈损)"
        
        lines.append(f"- [{action_str}] 数量: {amt} @ 价格: {price}")
        
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
    """
    将市场数据转换为 LLM 友好的结构化纯文本格式
    (已升级：支持 MACD/KDJ/BB/Trend 显示)
    """
    def fmt_price(price):
        if price is None or price == 0: return "0"
        # 简单处理，因为 market_data.py 已经做过 smart_fmt
        return str(price)

    def fmt_num(num):
        if num > 1_000_000_000: return f"{num/1_000_000_000:.1f}B"
        if num > 1_000_000: return f"{num/1_000_000:.1f}M"
        if num > 1_000: return f"{num/1_000:.1f}K"
        return f"{num:.0f}"

    # ========== 市场快照 ==========
    current_price = data.get("current_price", 0)
    atr_15m = data.get("atr_15m", 0)
    sent = data.get("sentiment", {})
    funding = sent.get("funding_rate", 0) * 100 
    oi = fmt_num(sent.get("open_interest", 0))
    vol_24h = fmt_num(sent.get("24h_quote_vol", 0))
    
    output = [
        "【市场快照】",
        f"• 当前价格: {current_price} | 15m ATR: {atr_15m}",
        f"• 资金费率: {funding:.4f}% | 未平仓合约: {oi} | 24h成交量: {vol_24h}",
        ""
    ]

    # ========== 按周期组织技术指标 ==========
    indicators = data.get("technical_indicators", {})
    timeframes = ['5m', '15m', '1h', '4h', '1d', '1w']
    
    for tf in timeframes:
        if tf not in indicators: continue
        d = indicators[tf]
        
        output.append(f"【{tf}周期】")
        
        # 1. 核心与趋势
        tf_price = d.get('price', 0)
        atr = d.get('atr', 0)
        rsi = d.get('rsi', 0)
        trend = d.get('trend_status', 'N/A')
        vol_stat = d.get('volume_status', 'N/A')
        
        output.append(f"• 状态: 价格={tf_price} | 趋势={trend} | ATR={atr} | Vol={vol_stat}")
        
        # 2. 震荡指标 (RSI + KDJ)
        kdj = d.get('kdj', {})
        k, _d, j = kdj.get('k', 0), kdj.get('d', 0), kdj.get('j', 0)
        output.append(f"• 震荡: RSI={rsi} | KDJ: K={k} D={_d} J={j}")

        # 3. 动能 (MACD)
        macd = d.get('macd', {})
        diff, dea, hist = macd.get('diff', 0), macd.get('dea', 0), macd.get('hist', 0)
        output.append(f"• MACD: Diff={diff} DEA={dea} Hist={hist}")

        # 4. 布林带
        bb = d.get('bollinger', {})
        up, mid, low, width = bb.get('up',0), bb.get('mid',0), bb.get('low',0), bb.get('width',0)
        output.append(f"• 布林带: Up={up} Low={low} Width={width}")
        
        # 5. K线序列
        closes = d.get('recent_closes', [])
        highs = d.get('recent_highs', [])
        lows = d.get('recent_lows', [])
        if closes:
            c_str = ", ".join([str(x) for x in closes])
            h_str = ", ".join([str(x) for x in highs])
            l_str = ", ".join([str(x) for x in lows])
            output.append(f"• 近5根K线: Close[{c_str}] High[{h_str}] Low[{l_str}]")
        
        # 6. EMA
        ema = d.get('ema', {})
        e20, e50, e100, e200 = ema.get('ema_20', 0), ema.get('ema_50', 0), ema.get('ema_100', 0), ema.get('ema_200', 0)
        output.append(f"• EMA: 20={e20} / 50={e50} / 100={e100} / 200={e200}")
        
        # 7. 价值分布
        vp = d.get('vp', {})
        poc = vp.get('poc', 0)
        val, vah = vp.get('val', 0), vp.get('vah', 0)
        # 已经是排好序的数值列表，直接取前3
        raw_hvns = vp.get('hvns', [])
        hvn_str = ", ".join([str(x) for x in raw_hvns[:3]]) if raw_hvns else "N/A"
        
        output.append(f"• 价值区: POC={poc} | VA=[{val}~{vah}] | HVN: {hvn_str}")
        output.append("")

    return "\n".join(output).strip()


def format_market_data_to_markdown(data: dict) -> str:
    """
    将复杂的市场 JSON 数据转换为 Markdown 表格
    (已升级：支持 MACD/KDJ/BB/Trend)
    """
    def fmt_price(price):
        return str(price)

    def fmt_num(num):
        if num > 1_000_000_000: return f"{num/1_000_000_000:.1f}B"
        if num > 1_000_000: return f"{num/1_000_000:.1f}M"
        if num > 1_000: return f"{num/1_000:.1f}K"
        return f"{num:.0f}"

    current_price = data.get("current_price", 0)
    atr_15m = data.get("atr_15m", 0)
    sent = data.get("sentiment", {})
    funding = sent.get("funding_rate", 0) * 100 
    
    header = (
        f"**Snapshot** | Price: {current_price} | 15m ATR: {atr_15m}\n"
        f"Sentiment: Fund: {funding:.4f}% | Vol24h: {fmt_num(sent.get('24h_quote_vol', 0))}\n"
    )

    # 扩展表头
    table_header = (
        "| TF | Trend | RSI/KDJ | MACD (Diff/Hist) | BB Width | 5 Closes | EMA (20/50/200) | POC | HVN |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
    )
    
    rows = []
    indicators = data.get("technical_indicators", {})
    timeframes = ['5m', '15m', '1h', '4h', '1d', '1w']
    
    for tf in timeframes:
        if tf not in indicators: continue
        d = indicators[tf]
        
        # 基础
        trend = d.get('trend_status', 'N/A')
        
        # 震荡
        rsi = d.get('rsi', 0)
        kdj = d.get('kdj', {})
        k, j = kdj.get('k',0), kdj.get('j',0)
        osc_str = f"RSI:{rsi} K:{k} J:{j}"
        
        # MACD
        macd = d.get('macd', {})
        diff, hist = macd.get('diff', 0), macd.get('hist', 0)
        macd_str = f"{diff}/{hist}"
        
        # BB
        bb = d.get('bollinger', {})
        width = bb.get('width', 0)
        
        # K线
        closes = d.get('recent_closes', [])
        c_str = ",".join([str(x) for x in closes])
        
        # EMA
        ema = d.get('ema', {})
        e_str = f"{ema.get('ema_20')}/{ema.get('ema_50')}/{ema.get('ema_200')}"
        
        # VP
        vp = d.get('vp', {})
        poc = vp.get('poc', 0)
        hvns = vp.get('hvns', [])[:3]
        h_str = ",".join([str(x) for x in hvns])
        
        row = f"| {tf} | {trend} | {osc_str} | {macd_str} | {width} | {c_str} | {e_str} | {poc} | {h_str} |"
        rows.append(row)
    
    return header + table_header + "\n".join(rows)