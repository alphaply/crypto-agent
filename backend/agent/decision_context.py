"""Read task memory and render decision prompts without retired daily context."""
from dataclasses import dataclass
import re
from string import Formatter

from backend.database import get_recent_summary_logic, get_short_memories
from backend.database_rules import format_trading_rules_context
from backend.utils.logger import setup_logger
from backend.utils.prompt_utils import render_prompt

logger = setup_logger(__name__)


def format_short_memory_text(config_id: str, limit: int = 1) -> str:
    memories = get_short_memories(config_id, limit=limit)
    return "\n\n".join(
        f"[updated={item.get('bucket_start')}] sources={item.get('source_count', 0)}\n"
        f"{item.get('market_summary') or '-'}" for item in memories
    ) or "(No short-term memory yet)"


def format_short_memory_for_llm(config_id: str, limit: int = 1, *, include_metadata: bool = False) -> str:
    memories = get_short_memories(config_id, limit=limit)
    entries = [
        (f"[覆盖 {item.get('window_start') or item.get('bucket_start')} → {item.get('window_end') or item.get('bucket_end')}；更新 {item.get('created_at')}]\n" if include_metadata else '')
        + str(item.get('market_summary') or '').strip()
        for item in memories if str(item.get('market_summary') or '').strip()
    ]
    return '\n\n'.join(entries) or '(No short-term memory yet)'


def format_recent_decisions(config_id: str) -> str:
    """Read complete summaries for the latest three runs in chronological order."""
    rows = get_recent_summary_logic(config_id, limit=3)
    return '\n'.join(
        f"[{row.get('timestamp')}] {str(row['strategy_logic']).strip()}"
        for row in reversed(rows) if str(row.get('strategy_logic') or '').strip()
    ) or '(暂无近期决策摘要)'


@dataclass(frozen=True)
class DecisionMemory:
    short_memory_text: str
    recent_summaries_text: str
    trading_rules_text: str


def load_decision_memory(config_id: str) -> DecisionMemory:
    """Keep each local source available even when another source fails."""
    sources = (
        ('short_memory_text', lambda: format_short_memory_for_llm(config_id, include_metadata=True), '(短期记忆读取失败)'),
        ('recent_summaries_text', lambda: format_recent_decisions(config_id), '(近期摘要读取失败)'),
        ('trading_rules_text', lambda: format_trading_rules_context(config_id), '(交易规则读取失败；不得假设没有规则)'),
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
_DAILY_TITLE = re.compile(r'^(?:daily\s+memory|每日记忆|日报记忆|每日总结)$', re.IGNORECASE)


def clean_decision_template(template: str) -> str:
    """Retire known daily sections at render time, preserving custom source files."""
    lines = []
    retired_level = None
    for line in template.splitlines():
        heading = _HEADING.match(line)
        if heading:
            level, title = len(heading[1]), heading[2]
            if retired_level is not None and level <= retired_level:
                retired_level = None
            if _DAILY_TITLE.fullmatch(title):
                retired_level = level if retired_level is None else min(level, retired_level)
        if retired_level is None:
            lines.append(line)
    cleaned = '\n'.join(lines)
    # An older user template may rename the heading while keeping only the
    # retired placeholder underneath it. Drop that otherwise empty section too.
    cleaned = re.sub(
        r'(?m)^ {0,3}#{1,6}[^\n]+\n[ \t]*\{history_text\}[ \t]*'
        r'(?:\n[ \t]*\(暂无历史记录\)[ \t]*)?(?=\n|$)', '', cleaned,
    )
    return cleaned.strip()


def prompt_fields(template: str) -> set[str]:
    """Distinguish actual placeholders from escaped braces in custom templates."""
    return {field for _, field, _, _ in Formatter().parse(template) if field is not None}


def render_decision_prompt(template: str, memory: DecisionMemory, **values) -> str:
    template = clean_decision_template(template)
    fields = prompt_fields(template)
    # Retired variables resolve to empty text even in old inline custom templates.
    values.update(history_text='', **vars(memory))
    prompt = render_prompt(template, **values).strip()
    sections = (
        ('short_memory_text', 'Short-term memory'),
        ('recent_summaries_text', '最近三轮决策摘要（历史计划，不是成交证明）'),
        ('trading_rules_text', '长期交易规则'),
    )
    for field, title in sections:
        if field not in fields:
            prompt += f'\n\n## {title}\n{getattr(memory, field)}'
    return prompt
