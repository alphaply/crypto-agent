export function parseReasoningTimestamp(value) {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  const normalized = typeof value === 'string' && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(value)
    ? value.replace(' ', 'T')
    : value;
  const parsed = new Date(normalized).getTime();
  return Number.isFinite(parsed) ? parsed : null;
}

export function formatReasoningElapsed(elapsedMs, locale = 'en') {
  const totalSeconds = Math.max(0, Math.floor(Number(elapsedMs || 0) / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (locale === 'zh') {
    return minutes ? `${minutes} 分 ${String(seconds).padStart(2, '0')} 秒` : `${totalSeconds} 秒`;
  }
  return minutes ? `${minutes}m ${String(seconds).padStart(2, '0')}s` : `${totalSeconds}s`;
}

export function formatReasoningSize(reasoningTokens, characterCount, locale = 'en') {
  const tokens = Math.max(0, Number(reasoningTokens || 0));
  const characters = Math.max(0, Number(characterCount || 0));
  const formatter = new Intl.NumberFormat(locale === 'zh' ? 'zh-CN' : 'en-US');
  if (tokens > 0) return `${formatter.format(tokens)} tokens`;
  return locale === 'zh'
    ? `${formatter.format(characters)} 字符`
    : `${formatter.format(characters)} characters`;
}
