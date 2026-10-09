"""Repair identified hard-clipped journals while preserving model summaries."""

LEGACY_EXCERPT = '【决策摘录】'
LEGACY_CLIP = '…（原文截取，非完整总结）'
DECISION_TEXT = '【决策原文】'


def restore_memory_source(row) -> dict:
    """Keep model summaries intact; recover only missing or hard-clipped logic."""
    result = dict(row)
    source = str(result.pop('_source_content', '') or '')
    logic = str(result.get('strategy_logic') or '')
    is_journal = logic.startswith(('【执行】', '【执行状态（非成交证明）】'))
    if is_journal and f'\n{DECISION_TEXT}\n' in logic:
        return result
    if not source.strip() or source == logic:
        return result
    if not logic.strip():
        result['strategy_logic'] = DECISION_TEXT + '\n' + source
        return result
    if not is_journal or LEGACY_CLIP not in logic:
        return result

    if LEGACY_EXCERPT in logic:
        execution, _ = logic.split(LEGACY_EXCERPT, 1)
        notes = []
        if execution.startswith('【执行状态（非成交证明）】'):
            notes.append('【历史回执说明】旧版仅保存了选定回执，完整工具回执无法由分析正文恢复；以下分析不作为额外成交证明。')
        if LEGACY_CLIP in execution:
            notes.append('【历史回执说明】上述旧工具回执自身已被截断，缺失部分未恢复，不得将其视为完整回执。')
        result['strategy_logic'] = '\n'.join([execution.rstrip(), *notes, DECISION_TEXT, source])
    else:
        notes = ['【历史策略摘要（原记录，非完整工具回执）】', logic]
        notes.append('【历史回执说明】上述旧执行记录存在截断，缺失的工具回执不能由分析正文恢复。')
        result['strategy_logic'] = '\n'.join([
            *notes,
            '【历史来源说明】下列正文来自同轮保存的完整分析；计划与模型表述不替代真实工具回执。',
            DECISION_TEXT, source,
        ])
    return result
