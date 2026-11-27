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


def _format_event_time(iso_str: str | None) -> str:
    if not iso_str:
        return '未知'
    try:
        dt = datetime.fromisoformat(str(iso_str).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = TZ_CN.localize(dt)
        else:
            dt = dt.astimezone(TZ_CN)
        return dt.strftime('%m-%d %H:%M')
    except Exception:
        return '未知'


def _format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return '未知'
    secs = max(0, int(round(float(seconds))))
    if secs < 60:
        return f'{secs}s'
    if secs < 3600:
        return f'{secs // 60}m'
    if secs < 86400:
        return f'{secs / 3600:.1f}h'
    return f'{secs / 86400:.1f}d'


def _weighted_avg_price(events: list[dict], kinds: set[str]) -> float | None:
    total_qty = 0.0
    total_notional = 0.0
    for event in events:
        if event.get('kind') not in kinds:
            continue
        qty = event.get('quantity')
        price = event.get('price')
        if qty is None or price is None or qty <= 0:
            continue
        total_qty += float(qty)
        total_notional += float(qty) * float(price)
    return (total_notional / total_qty) if total_qty > 0 else None


def _consecutive_streak(positions: list[dict]) -> tuple[str | None, int]:
    streak_type = None
    count = 0
    for pos in positions:
        pnl = pos.get('realized_net_pnl')
        if pnl is None:
            pnl = pos.get('realized_pnl_before_fees')
        if pnl is None or abs(pnl) <= 1e-10:
            break
        kind = 'loss' if pnl < 0 else 'win'
        if streak_type is None:
            streak_type = kind
            count = 1
        elif streak_type == kind:
            count += 1
        else:
            break
    return streak_type, count


def _format_recent_closed_positions(config_id: str, agent_config: dict, *, days: int = 7, limit: int | None = None) -> str:
    """Format recent closed positions for this task only, without raw ledger dumps."""
    try:
        from backend.app.services.position_history_service import read_position_history
        now = datetime.now(TZ_CN)
        cutoff_ms = int((now - timedelta(days=days)).timestamp() * 1000)
        data = read_position_history(config_id, cfg=agent_config, now=now)
        closed = []
        for pos in data.get('positions') or []:
            if pos.get('status') != 'closed':
                continue
            closed_at = pos.get('closed_at')
            if not closed_at:
                continue
            try:
                closed_dt = datetime.fromisoformat(str(closed_at).replace('Z', '+00:00'))
                if closed_dt.tzinfo is None:
                    closed_dt = TZ_CN.localize(closed_dt)
                closed_ms = int(closed_dt.timestamp() * 1000)
            except Exception:
                continue
            if closed_ms >= cutoff_ms:
                closed.append((closed_ms, pos))
        closed.sort(key=lambda item: item[0], reverse=True)
        selected = closed[:limit] if limit is not None else closed
        recent = [pos for _, pos in selected]
        if not recent:
            return f'最近{days}天已平仓持仓明细（仅当前任务）：暂无已确认完整平仓记录。'

        lines = [f'最近{days}天已平仓持仓明细（仅当前任务，共{len(recent)}笔，按平仓时间倒序）：']
        streak_type, streak_count = _consecutive_streak(recent)
        if streak_type == 'loss' and streak_count >= 2:
            lines.append(f'近期平仓走势：最近连续亏损 {streak_count} 笔。')
        elif streak_type == 'win' and streak_count >= 2:
            lines.append(f'近期平仓走势：最近连续盈利 {streak_count} 笔。')

        side_labels = {'LONG': '多', 'SHORT': '空', 'SPOT': '现货'}
        for pos in recent:
            events = pos.get('events') or []
            entry_avg = _weighted_avg_price(events, {'entry', 'add'})
            exit_avg = _weighted_avg_price(events, {'exit', 'reduce', 'close'})
            entry_str = f'{entry_avg:.4g}' if entry_avg is not None else '未知'
            exit_str = f'{exit_avg:.4g}' if exit_avg is not None else '未知'
            currency = pos.get('currency') or 'USDT'
            net_pnl = pos.get('realized_net_pnl')
            gross_pnl = pos.get('realized_pnl_before_fees')
            if net_pnl is not None:
                pnl_str = f'净盈亏 {net_pnl:+.2f} {currency}'
            elif gross_pnl is not None:
                pnl_str = f'费前盈亏 {gross_pnl:+.2f} {currency}（手续费未知）'
            else:
                pnl_str = '盈亏未知'
            side_label = side_labels.get(pos.get('side'), pos.get('side') or '未知')
            exit_events = [e for e in events if e.get('kind') in {'exit', 'reduce', 'close'}]
            reason_raw = next((str(e.get('reason')).strip() for e in reversed(exit_events) if e.get('reason')), '')
            reason_clean = ' '.join(reason_raw.split())[:60] if reason_raw else ''
            reason_part = f' | 理由: {reason_clean}' if reason_clean else ''
            lines.append(
                f"- [{_format_event_time(pos.get('opened_at'))} -> {_format_event_time(pos.get('closed_at'))}] "
                f"{pos.get('symbol')} {side_label} | 均价 {entry_str} -> {exit_str} | "
                f"持仓 {_format_duration(pos.get('holding_seconds'))} | {pnl_str}{reason_part}"
            )
        return '\n'.join(lines)
    except Exception as exc:
        logger.warning('Failed to format recent closed positions for memory (%s): %s', config_id, exc)
        return ''


def format_recent_position_history_for_memory(config_id: str, agent_config: dict) -> str:
    """One bounded task-scoped statistics block plus recent closed positions; no full ledger."""
    from backend.utils.task_performance import task_history_context
    stats = task_history_context(config_id, agent_config)
    positions = _format_recent_closed_positions(config_id, agent_config)
    return f'{stats}\n\n{positions}' if positions else stats


def format_memory_evidence(evidence: str) -> str:
    return ('\n\n最近7天与全部历史统计及近期已平仓持仓（仅当前任务；不是实时持仓；缺失事实不补零）：\n' + evidence) if evidence else ''


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
