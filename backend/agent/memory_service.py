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
    """Historical ledgers are on-demand evidence, never consolidation input."""
    return ''


def format_memory_evidence(evidence: str) -> str:
    # Compatibility entry point: performance and ledgers have explicit detail APIs.
    return ''


def organize_memory(source: str, agent_config: dict, *, operation_id: str) -> str:
    """Consolidate short-term memory; automatic rule maintenance is suspended."""
    from backend.agent.memory_agent import run_memory_review
    from backend.agent.context_projections import persist_projections

    from backend.agent.strategy_material import strategy_material
    source = strategy_material(source) or '暂无有效策略素材。'
    _projection_metadata.set(None)
    stored = get_review_result(operation_id)
    from backend.agent.context_projections import VERSION
    from backend.agent.strategy_material import validate_strategy_text
    compatible = stored and (stored['status'] == 'partial' or
        (not stored.get('projection_metadata') and validate_strategy_text(stored.get('summary', ''))) or
        (stored.get('projection_metadata') or {}).get('projection_version') == VERSION)
    if stored and not compatible:
        operation_id += f':strategy-v{VERSION}'
        stored = get_review_result(operation_id)
    if stored:
        verify_memory_lease()
        if stored['config_id'] != agent_config['config_id']:
            raise MemoryReviewError('Memory operation belongs to another task')
        metadata = stored.get('projection_metadata') or {}
        persist_projections(agent_config['config_id'], metadata)
        _projection_metadata.set(metadata)
        return stored['summary'] if stored['status'] == 'completed' else ''
    result = run_memory_review(source, agent_config, operation_id=operation_id)
    if result.status in {'completed', 'partial'}:
        save_review_result(agent_config['config_id'], operation_id, result)
    if result.status == 'completed':
        verify_memory_lease()
        metadata = result.projection_metadata
        persist_projections(agent_config['config_id'], metadata)
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
    markers = (
        "Window:",
        "Symbol:",
        "Previous rolling memory:",
        "Previous short memory:",
        "Recent strategy summaries:",
        "Recent market/agent reasoning:",
    )
    if any(marker in text for marker in markers):
        return True
    return text.endswith("...") and source_input.startswith(text[:-3])


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
    rows = get_summary_logic_between(config_id, since_time, end_time)
    if limit is not None:
        if limit < 1:
            raise ValueError('Rolling memory row limit must be positive')
        rows = rows[-limit:]
    if not rows:
        return False

    previous = format_short_memory_for_llm(config_id, limit=1)
    source_text = "\n".join(
        f"[{row.get('timestamp')}] {row.get('strategy_logic')}"
        for row in rows
        if row.get("strategy_logic")
    )
    memory_input = (
        f"Window: last {hours}h\n"
        f"Symbol: {target_config.get('symbol')}\n\n"
        f"Previous rolling memory:\n{previous}\n\n"
        f"Recent strategy summaries:\n{source_text}"
    )
    pos_history_text = format_recent_position_history_for_memory(config_id, target_config)
    memory_input += format_memory_evidence(pos_history_text)
    memory_summary = organize_memory(
        memory_input, {**target_config, 'config_id': config_id},
        operation_id=f'rolling:{config_id}:{rows[-1].get("timestamp")}')
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
