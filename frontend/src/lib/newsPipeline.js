export const NEWS_STAGES = ['collect', 'polymarket_review', 'score', 'filter', 'summarize', 'publish', 'complete'];

export function newsErrorExplanation(error, details = {}, locale = 'zh') {
  if (locale !== 'zh') return error;
  details ||= {};
  const value = String(error || '');
  if (/News digest exceeds 800 characters/.test(value)) return '摘要超过了旧版设置的800字上限，该轮结果未发布。';
  if (/Invalid news digest reference/.test(value)) return '摘要里的来源编号无效，无法对应到本轮新闻。';
  if (/Invalid news digest text/.test(value)) return '摘要正文包含不符合要求的引用或链接格式。';
  if (/Invalid news digest (structure|section|item)/.test(value)) return '摘要的段落结构或事件格式不符合要求。';
  if (/Empty news model response/.test(value)) return '摘要模型返回了空内容。';
  if (details.code === 'timeout') return '模型请求超时，本轮未能完成。';
  if (details.code === 'rate_limit' || details.http_status === 429) return '模型服务限制了请求频率或额度，请检查服务商的额度。';
  if (details.code === 'auth_error' || details.http_status === 401) return '模型服务拒绝了身份验证，请检查该模型的 API Key。';
  if (details.code === 'permission_error' || details.http_status === 403) return '当前模型账号没有访问权限，请检查服务商权限。';
  if (details.code === 'invalid_response') return '模型已返回内容，但结果未通过校验。下方可查看具体原因和原始响应。';
  if (/News processing failed; retaining the previous snapshot/.test(value)) return '该历史批次只保存了通用错误提示，可打开本轮模型调用记录查看当时的具体原因。';
  return value;
}

const count = (value) => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;

export function pipelineCounts(run = {}) {
  const values = run.counts || {};
  return {
    fetched: count(values.fetched ?? run.candidate_count),
    candidates: count(values.candidates ?? run.candidate_count),
    scored: count(values.scored ?? run.scored_count),
    skipped: count(values.skipped),
    cached: count(values.cached),
    failed: count(values.failed ?? run.score_failures),
    selected: count(values.selected ?? run.selected_count),
    filtered: count(values.filtered ?? run.filtered_count),
    sources_total: count(values.sources_total),
    sources_completed: count(values.sources_completed),
  };
}

export function pipelineProgress(run = {}) {
  const values = pipelineCounts(run);
  const total = count(run.progress?.total ?? values.candidates);
  const completed = Math.min(total, count(run.progress?.completed ?? values.scored + values.failed + values.skipped));
  return { total, completed, percent: total ? Math.round(completed / total * 100) : 0 };
}

export function scoreDistribution(items = []) {
  const buckets = [0, 20, 40, 60, 80].map((minimum) => ({ label: minimum === 80 ? '80–100' : `${minimum}–<${minimum + 20}`, count: 0 }));
  for (const item of items) {
    if (item.score == null || !Number.isFinite(Number(item.score))) continue;
    const value = Number(item.score);
    if (value < 0 || value > 100) continue;
    buckets[Math.min(4, Math.floor(value / 20))].count += 1;
  }
  return buckets;
}

export function sourceDistribution(items = []) {
  const groups = new Map();
  for (const item of items) {
    const name = item.source || item.source_id || '—';
    if (!groups.has(name)) groups.set(name, { name, selected: 0, filtered: 0, failed: 0, pending: 0, scored: 0 });
    const group = groups.get(name);
    const status = ['selected', 'filtered', 'failed', 'scored'].includes(item.status) ? item.status : 'pending';
    group[status] += 1;
  }
  return [...groups.values()];
}

export function safeTraceUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : '';
  } catch { return ''; }
}

export function durationLabel(value) {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  const seconds = Math.max(0, Math.round(Number(value) / 1000));
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}
