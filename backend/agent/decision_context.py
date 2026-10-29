"""Read active task memory and remove retired prompt injections."""
from dataclasses import dataclass, field
import re
from string import Formatter

from backend.utils.logger import setup_logger
from backend.utils.prompt_utils import render_prompt
from backend.utils.event_time import EVENT_TIME_POLICY

logger = setup_logger(__name__)


def format_short_memory_text(config_id: str, limit: int = 1) -> str:
    return format_short_memory_for_llm(config_id, limit, include_metadata=True)


def format_short_memory_for_llm(config_id: str, limit: int = 1, *, include_metadata: bool = False) -> str:
    from backend.agent.context_projections import stored_sources
    from backend.utils.trade_evidence import configured_account_scope
    sources = stored_sources(config_id, configured_account_scope(config_id),
                             'short_memory', status='ready', limit=max(1, int(limit or 1)))
    memories = [{**source['row'], 'market_summary': source['projection']['strategy_text']}
                for source in sources]
    entries = [
        (f"[覆盖 {item.get('window_start') or item.get('bucket_start')} → {item.get('window_end') or item.get('bucket_end')}；更新 {item.get('created_at')}]\n" if include_metadata else '')
        + str(item.get('market_summary') or '').strip()
        for item in memories if str(item.get('market_summary') or '').strip()
    ]
    return '\n\n'.join(entries) or '(No short-term memory yet)'


def format_recent_decisions(config_id: str) -> str:
    """Read versioned strategy only; migration never exposes raw restrictions."""
    from backend.agent.context_projections import stored_sources
    from backend.utils.trade_evidence import configured_account_scope
    rows = stored_sources(config_id, configured_account_scope(config_id),
                          'strategy_summary', status='ready', limit=3)
    return '\n'.join(
        f"[{row.get('timestamp')}] {row['projection']['strategy_text']}"
        for row in reversed(rows) if row['projection']['strategy_text'].strip()
    ) or '(暂无近期决策摘要)'


@dataclass(frozen=True)
class DecisionMemory:
    short_memory_text: str
    recent_summaries_text: str
    trading_rules_text: str = ''  # Retired placeholder, retained for old callers/templates.
    execution_evidence: dict = field(default_factory=dict)


def load_decision_memory(config_id: str, agent_config: dict | None = None) -> DecisionMemory:
    """Keep each local source available even when another source fails."""
    from backend.config import config
    from backend.agent.context_projections import enabled, load_strategy_views
    cfg = agent_config if agent_config is not None else config.get_config_by_id(config_id)
    if enabled(cfg):
        try:
            short, recent, evidence = load_strategy_views(config_id)
            return DecisionMemory(short, recent, execution_evidence=evidence)
        except Exception as exc:
            logger.warning('Strategy projections unavailable for %s: %s', config_id, exc)
            return DecisionMemory('（已发布短期策略记忆读取失败）', '（已发布策略摘要读取失败）')
    sources = (
        ('short_memory_text', lambda: format_short_memory_for_llm(config_id, include_metadata=True), '(短期记忆读取失败)'),
        ('recent_summaries_text', lambda: format_recent_decisions(config_id), '(近期摘要读取失败)'),
    )
    values = {}
    for name, read, fallback in sources:
        try:
            values[name] = read()
        except Exception as exc:
            logger.warning('Unable to load %s for %s: %s', name, config_id, exc)
            values[name] = fallback
    return DecisionMemory(**values)


_HEADING = re.compile(r'^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$')
_RETIRED_TITLE = re.compile(
    r'^(?:daily\s+memory|每日记忆|日报记忆|每日总结|长期交易规则|有效长期规则|'
    r'long[- ]term\s+(?:trading\s+)?rules|当前小时级策略周期)$', re.IGNORECASE,
)


def clean_decision_template(template: str) -> str:
    """Remove known retired sections at render time, preserving custom source files."""
    lines = []
    retired_level = None
    for line in template.splitlines():
        heading = _HEADING.match(line)
        if heading:
            level, title = len(heading[1]), heading[2]
            if retired_level is not None and level <= retired_level:
                retired_level = None
            if _RETIRED_TITLE.fullmatch(title):
                retired_level = level if retired_level is None else min(level, retired_level)
        if retired_level is None:
            lines.append(line)
    cleaned = '\n'.join(lines)
    # An older user template may rename the heading while keeping only the
    # retired placeholder underneath it. Drop that otherwise empty section too.
    cleaned = re.sub(
        r'(?m)^ {0,3}#{1,6}[^\n]+\n[ \t]*\{(?:history_text|trading_rules_text)\}[ \t]*'
        r'(?:\n[ \t]*\(暂无历史记录\)[ \t]*)?(?=\n|$)', '', cleaned,
    )
    cleaned = re.sub(r'(?m)^当前退出模式：exit_mode=\w+。\s*$', '', cleaned)
    return cleaned.strip()


def prompt_fields(template: str) -> set[str]:
    """Distinguish actual placeholders from escaped braces in custom templates."""
    return {field for _, field, _, _ in Formatter().parse(template) if field is not None}


def render_decision_prompt(template: str, memory: DecisionMemory, **values) -> str:
    template = clean_decision_template(template)
    recent_title = ('近期已发布策略摘要（最多三条；历史计划，不是成交证明）'
                    if values.get('exit_mode') == 'independent_exits'
                    else '最近三轮决策摘要（历史计划，不是成交证明）')
    if values.get('exit_mode') == 'independent_exits':
        template = template.replace('最近三轮决策摘要（历史计划，不是成交证明）', recent_title)
    fields = prompt_fields(template)
    # Retired variables resolve to empty text even in old inline custom templates.
    values.update(history_text='', short_memory_text=memory.short_memory_text,
                  recent_summaries_text=memory.recent_summaries_text)
    values['trading_rules_text'] = ''
    prompt = render_prompt(template, **values).strip()
    sections = (
        ('short_memory_text', 'Short-term memory'),
        ('recent_summaries_text', recent_title),
    )
    for field, title in sections:
        if field not in fields:
            prompt += f'\n\n## {title}\n{getattr(memory, field)}'
    if values.get('exit_mode') == 'independent_exits':
        from backend.utils.trading_policy import INDEPENDENT_RUNTIME_POLICY
        prompt += '\n\n## 当前退出工具事实\n' + INDEPENDENT_RUNTIME_POLICY
    return prompt + '\n\n' + EVENT_TIME_POLICY
