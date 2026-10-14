"""Deterministic narrative filtering; originals and receipts stay in audit storage."""
import re

# Classify whole semantic clauses, never slice a strategy sentence to fit a budget.
DIAGNOSTIC = re.compile(
    r'operation_id|parent_operation_id|tool_call_id|toolu_|resolution|'
    r'independent_position_cycle_missing|close_position_|open_position_|Order rejected|执行状态|成交证明|rejected because|not_executed|failed\s*=|stop_on_error|completed\s*=|submitted\s*=|'
    r'active_protection|execution_review|position_cycles|window_complete|recent_fills|'
    r'归属未核验|归属校验|订单不属于|无活跃.*周期|本任务无自有持仓|账户持仓为|'
    r'周期.*(?:失败|报错|冲突|恢复|核验|闭合)|(?:恢复|解锁|核查).*操作|'
    r'Get_trade_operation|execute_trade_actions|Edit.*(?:拒|失败)|'
    r'未核验|待核验|unknown|unavailable|unresolved|blocked_actions|\ballowed\b|'
    r'\b(?:order|submission)\b[^。！？.!?；;\r\n]{0,60}\b(?:unverified|unconfirmed)\b|'
    r'\b(?:unverified|unconfirmed)\s+(?:(?:existing|original|pending|submitted)\s+)?(?:order|submission)\b|'
    r'\b(?:verify|reconcile)\s+(?:(?:the|an?|this|that|existing|original|pending|submitted)\s+){0,3}(?:order|submission)\b|'
    r'已解锁|全面放行|实际工具回执|结构化回执|执行结果|'
    r'持仓快照|当前持仓|三笔外部挂单|多仓@|\d+\s*(?:多仓|空仓)\s*@|'
    r'\d+(?:\.\d+)?\s*(?:多仓|空仓|多头仓位|空头仓位)|order_id|order_status|'
    r'\b(?:regular|algo|mock):[\w-]+|持仓周期|周期对账|周期恢复|周期缺失|'
    r'权益|收益率|回撤|已确认累计盈亏|手续费|归属成交|无法匹配|'
    r'胜率|盈亏比|(?:盈亏|盈利|亏损)\s*[:：=]\s*[+-]?\d|'
    r'完整平仓|战绩统计|样本\s*[:：]|统计起点|截至快照|同步\s*[:：]|'
    r'证据读取时间|策略投影来源|哈希|成交账本|窗口覆盖|未闭合周期|解除前|换ID|操作ID|外部挂单|订单快照|当前仓位|本任务无|订单ID|原操作|回执|执行解锁', re.I)


def strategy_material(text: str) -> str:
    """Keep judgments, price conditions and effective lessons in their own clauses."""
    # JSON is evidence plumbing, not narrative. Remove balanced objects/arrays
    # before clause classification (including pretty-printed multi-line data).
    clean, depth, quoted, escaped = [], 0, False, False
    value = str(text or '')
    for index, char in enumerate(value):
        if depth:
            if char == '"' and not escaped:
                quoted = not quoted
            if not quoted:
                depth += int(char in '{[') - int(char in '}]')
            escaped = char == '\\' and not escaped
            continue
        # Numeric price ranges such as [2385,2458] are strategy content.
        if char == '{' or (char == '[' and value[index + 1:].lstrip().startswith(('{', '[', '"'))):
            depth, quoted, escaped = 1, False, False
            continue
        clean.append(char)
    result, seen = [], set()
    for part in re.split(r'(?<=[。！？；;])|[\r\n]+', ''.join(clean)):
        part = part.strip()
        if not part or DIAGNOSTIC.search(part) or re.fullmatch(r'[#\s【】\-:：]+', part):
            continue
        if part not in seen:
            result.append(part)
            seen.add(part)
    return '\n'.join(result)


def validate_strategy_text(text: str) -> bool:
    return not DIAGNOSTIC.search(text) and not re.search(r'[{}]|\[\s*[{"\[]', text)
