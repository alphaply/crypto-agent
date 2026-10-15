"""Short-term memory evidence, consolidation and persistence, independent of trading graphs."""
from datetime import datetime, timedelta

import pytz

from backend.agent.decision_context import format_short_memory_for_llm
from backend.agent.memory_workflow import (
    serialized_memory_review, get_review_result, save_review_result, verify_memory_lease,
    _review_projection as _projection_metadata,
)
from backend.config import config as global_config
from backend.database import get_summary_logic_between, save_short_memory as _save_short_memory
from backend.utils.logger import setup_logger

TZ_CN = pytz.timezone(getattr(global_config, 'timezone', 'Asia/Shanghai'))
logger = setup_logger(__name__)


class MemoryReviewError(RuntimeError):
    """A failed consolidation that leaves the previous memory unchanged."""


def save_short_memory(*args, **kwargs):
    verify_memory_lease()
    metadata = _projection_metadata.get()
    if metadata:
        kwargs['decision_projection'] = metadata
    return _save_short_memory(*args, **kwargs)


def format_recent_position_history_for_memory(config_id: str, agent_config: dict) -> str:
    """Restore only the old seven-day closed-position/fill evidence."""
    from backend import database
    symbol = agent_config.get('symbol', '')
    mode = str(agent_config.get('mode') or 'STRATEGY').upper()
    if mode not in {'REAL', 'STRATEGY'}:
        return ''
    lines = [f"证据读取时间：{datetime.now(TZ_CN).strftime('%Y-%m-%d %H:%M:%S %Z')}"]
    try:
        rows = database.get_closed_positions_7d(config_id, symbol=symbol, days=7, mode=mode)
        lines.append(database.format_closed_positions_summary(rows, days=7))
    except Exception:
        logger.exception('Seven-day closed positions unavailable for %s', config_id)
        lines.append('7天平仓记录读取失败，不能据此断言没有交易或收益为零。')
    if mode == 'REAL':
        from backend.utils.execution_ledger import recent_activity_summary
        try:
            lines.append(recent_activity_summary(config_id, symbol))
        except Exception:
            logger.exception('Seven-day fills unavailable for %s', config_id)
            lines.append('最近7天成交活动暂不可用，不能据此断言历史完整。')
    return '\n'.join(lines)


def format_memory_evidence(evidence: str) -> str:
    return ('\n\n最近7天操作记录（成交/平仓，不是实时持仓；缺失事实不补零）：\n' + evidence) if evidence else ''


def build_memory_input(rows, previous: str, evidence: str, beginning: str, end: str) -> str:
    entries = [f"[{row['timestamp']}] {row['strategy_logic']}" for row in rows if row['strategy_logic']]
    return (f'过去4h滚动窗口：{beginning} → {end}\n上一轮短期记忆：\n{previous}'
            + '\n本轮及前4h策略压缩摘要：\n' + '\n'.join(entries) + format_memory_evidence(evidence))


def organize_memory(source: str, agent_config: dict, *, operation_id: str, source_metadata=None) -> str:
    """Consolidate short-term memory; automatic rule maintenance is suspended."""
    from backend.agent.memory_agent import run_memory_review
    from backend.agent.context_projections import capture_sources

    _projection_metadata.set(None)
    stored = get_review_result(operation_id)
    if stored:
        verify_memory_lease()
        if stored['config_id'] != agent_config['config_id']:
            raise MemoryReviewError('Memory operation belongs to another task')
        metadata = stored.get('projection_metadata') or {}
        _projection_metadata.set(metadata)
        return stored['summary'] if stored['status'] == 'completed' else ''
    captured = source_metadata if source_metadata is not None else capture_sources(agent_config['config_id'])
    result = run_memory_review(source, agent_config, operation_id=operation_id)
    result.projection_metadata = captured
    if result.status in {'completed', 'partial'}:
        save_review_result(agent_config['config_id'], operation_id, result)
    if result.status == 'completed':
        verify_memory_lease()
        metadata = result.projection_metadata
        _projection_metadata.set(metadata)
        return result.summary
    logger.warning('Memory review %s: %s', result.status, result.error)
    if result.status == 'partial':
        return ''  # Legacy rule receipts remain non-retryable historical data.
    raise MemoryReviewError(result.error or 'Memory review failed; previous memory retained')


def is_invalid_memory(summary: str, source_input: str) -> bool:
    text = str(summary or "").strip()
    if not text:
        return True
    return False


@serialized_memory_review
def generate_rolling_short_memory_for_config(
    config_id: str,
    agent_config: dict | None = None,
    now_cn: datetime | None = None,
    hours: int = 4,
    limit: int | None = None,
) -> bool:
    """Review the complete time window unless an explicit row limit is requested."""
    now_cn = now_cn or datetime.now(TZ_CN)
    all_configs = global_config.get_all_symbol_configs()
    target_config = agent_config or next((c for c in all_configs if c.get("config_id") == config_id), None)
    if not target_config or not target_config.get("enabled", True):
        return False

    since_time = (now_cn - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    # Stored decision timestamps have second precision. Include the current
    # second while excluding records beyond the requested review window.
    end_time = (now_cn + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    from backend.agent.context_projections import memory_summary_rows
    rows = memory_summary_rows(config_id, get_summary_logic_between(config_id, since_time, end_time))
    if limit is not None:
        if limit < 1:
            raise ValueError('Rolling memory row limit must be positive')
        rows = rows[-limit:]
    if not rows:
        return False

    from backend.agent.context_projections import capture_sources
    captured = capture_sources(config_id, summary_rows=rows)
    previous = format_short_memory_for_llm(config_id, limit=1)
    pos_history_text = format_recent_position_history_for_memory(config_id, target_config)
    memory_input = build_memory_input(rows, previous, pos_history_text, since_time, now_cn.strftime('%Y-%m-%d %H:%M:%S'))
    memory_summary = organize_memory(
        memory_input, {**target_config, 'config_id': config_id},
        operation_id=f'rolling:{config_id}:{rows[-1].get("timestamp")}', source_metadata=captured)
    if is_invalid_memory(memory_summary, memory_input):
        logger.warning(f"Skip saving invalid rolling short memory for {config_id}.")
        return False
    end_stamp = now_cn.strftime("%Y-%m-%d %H:%M:%S")
    save_short_memory(
        # Keep the legacy edit key; explicit metadata describes the evidence window.
        end_stamp,
        end_stamp,
        target_config.get("symbol", "Unknown"),
        config_id,
        memory_summary,
        pos_history_text,
        len(rows),
        window_start=since_time,
        window_end=end_stamp,
        source_summary_ids=[row['id'] for row in rows if row.get('id') is not None],
        run_id=f'rolling:{config_id}:{rows[-1].get("timestamp")}',
    )
    return True
