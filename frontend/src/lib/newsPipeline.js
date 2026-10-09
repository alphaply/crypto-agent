export const NEWS_STAGES = ['collect', 'score', 'filter', 'summarize', 'publish', 'complete'];

const count = (value) => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;

export function pipelineCounts(run = {}) {
  const values = run.counts || {};
  return {
    fetched: count(values.fetched ?? run.candidate_count),
    candidates: count(values.candidates ?? run.candidate_count),
    scored: count(values.scored ?? run.scored_count),
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
  const completed = Math.min(total, count(run.progress?.completed ?? values.scored + values.failed));
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
