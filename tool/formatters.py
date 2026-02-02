"""
格式化工具函数模块
将复杂的数据结构转换为 Agent 易读的文本格式
"""

def format_positions_to_agent_friendly(positions: list) -> str:
    """
    将复杂的持仓 JSON 转换为 Agent 易读的精简文本
    """
    if not positions:
        return "无持仓 (No Positions)"
    
    lines = []
    for p in positions:
        side = p.get('side', '').upper()
        # 清理 symbol 名字，比如 BNB/USDT:USDT -> BNB/USDT
        sym = p.get('symbol', '').split(':')[0]
        amt = float(p.get('amount', 0))
        entry = float(p.get('entry_price', 0))
        pnl = float(p.get('unrealized_pnl', 0))
        
        pnl_sign = "+" if pnl >= 0 else ""
        
        line = f"[{side}] {sym} | Amt: {amt} | Entry: {entry} | PnL: {pnl_sign}{pnl:.3f}"
        lines.append(line)
        
    return "\n".join(lines)


def format_orders_to_agent_friendly(orders: list) -> str:
    """
    将活跃挂单转换为 Agent 易读的精简文本
    """
    if not orders:
        return "无活跃挂单 (No Active Orders)"

    lines = []
    for o in orders:
        # 1. 提取方向
        side = o.get('side', '').upper()
        
        # 2. 标准化类型 (处理中文 "限价入场")
        raw_type = str(o.get('type', 'LIMIT'))
        if '限价' in raw_type or 'limit' in raw_type.lower():
            order_type = 'LIMIT'
        else:
            order_type = raw_type.upper()

        # 3. 提取核心数据
        oid = o.get('id', 'N/A')
        price = float(o.get('price', 0))
        amt = float(o.get('amount', 0))

        # 4. 可选: 止盈止损 (策略单可能会有)
        tp = float(o.get('tp', 0) or o.get('take_profit', 0))
        sl = float(o.get('sl', 0) or o.get('stop_loss', 0))
        
        extras = ""
        if tp > 0 or sl > 0:
            extras = f" | TP: {tp} | SL: {sl}"
        
        line = f"[{side}] {order_type} | ID: '{oid}' | Price: {price} | Amt: {amt}{extras}"
        lines.append(line)

    return "\n".join(lines)


def format_market_data_to_markdown(data: dict) -> str:
    """
    将复杂的市场 JSON 数据转换为 LLM 易读的 Markdown 格式
    (更新：新增 Recent Highs/Lows 列)
    """
    def fmt_price(price):
        if price is None or price == 0: return "0"
        abs_p = abs(price)
        if abs_p >= 1000: return f"{int(price)}"      
        if abs_p >= 1: return f"{price:.2f}"          
        if abs_p >= 0.01: return f"{price:.4f}"       
        return f"{price:.8f}".rstrip('0')              

    def fmt_num(num):
        if num > 1_000_000_000: return f"{num/1_000_000_000:.1f}B"
        if num > 1_000_000: return f"{num/1_000_000:.1f}M"
        if num > 1_000: return f"{num/1_000:.1f}K"
        return f"{num:.0f}"

    current_price = data.get("current_price", 0)
    atr_15m = data.get("atr_15m", 0)
    
    sent = data.get("sentiment", {})
    funding = sent.get("funding_rate", 0) * 100 
    oi = sent.get("open_interest", 0)
    
    vol_24h = fmt_num(sent.get("24h_quote_vol", 0))
    oi_str = fmt_num(oi)
    
    header = (
        f"**Snapshot** | Price: {fmt_price(current_price)} | 15m ATR: {fmt_price(atr_15m)}\n"
        f"Sentiment: Fund: {funding:.4f}% | OI: {oi_str} | Vol24h: {vol_24h}\n"
    )

    # ------------------ 修改点 1：表头增加 Highs 和 Lows ------------------
    table_header = (
        "| TF | Price | ATR | RSI | Vol Status | Last 5 Closes | Last 5 Highs | Last 5 Lows | EMA (20/50/100/200) | POC | VA Range | HVN |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    
    rows = []
    indicators = data.get("technical_indicators", {})
    all_possible_timeframes = ['5m', '15m', '1h', '4h', '1d', '1w']
    
    for tf in all_possible_timeframes:
        if tf not in indicators: continue
        d = indicators[tf]
        
        # 1. 基础数据
        tf_price = fmt_price(d.get('price', 0))
        atr = fmt_price(d.get('atr', 0))
        rsi = f"{d.get('rsi', 0):.1f}"
        
        # 2. 成交量状态
        vol_stat = d.get('volume_status', 'N/A')
        
        # ------------------ 修改点 2：提取并格式化 Highs 和 Lows ------------------
        raw_closes = d.get('recent_closes', [])
        closes_str = ", ".join([fmt_price(x) for x in raw_closes])
        
        raw_highs = d.get('recent_highs', [])
        highs_str = ", ".join([fmt_price(x) for x in raw_highs])

        raw_lows = d.get('recent_lows', [])
        lows_str = ", ".join([fmt_price(x) for x in raw_lows])
        
        # 4. EMA
        ema = d.get('ema', {})
        e20 = fmt_price(ema.get('ema_20', 0))
        e50 = fmt_price(ema.get('ema_50', 0))
        e100 = fmt_price(ema.get('ema_100', 0))
        e200 = fmt_price(ema.get('ema_200', 0))
        ema_str = f"{e20}/{e50}/{e100}/{e200}"
        
        # 5. VP 数据
        vp = d.get('vp', {})
        poc = fmt_price(vp.get('poc', 0))
        val = fmt_price(vp.get('val', 0))
        vah = fmt_price(vp.get('vah', 0))
        va_range = f"{val}-{vah}"
        
        raw_hvns = vp.get('hvns', [])
        top_hvns = sorted(raw_hvns, reverse=True)[:3]
        hvn_str = ",".join([fmt_price(h) for h in top_hvns])
        
        # ------------------ 修改点 3：将新数据加入行中 ------------------
        row = f"| {tf} | {tf_price} | {atr} | {rsi} | {vol_stat} | {closes_str} | {highs_str} | {lows_str} | {ema_str} | {poc} | {va_range} | {hvn_str} |"
        rows.append(row)
    
    return header + table_header + "\n".join(rows)