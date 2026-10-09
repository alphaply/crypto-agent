import test from 'node:test';
import assert from 'node:assert/strict';
import { pipelineCounts, pipelineProgress, scoreDistribution, sourceDistribution, safeTraceUrl, durationLabel } from '../src/lib/newsPipeline.js';
import { JEV_PROVIDER_PRESETS, savedScorerConnection } from '../src/lib/scorerProvider.js';

test('pipeline progress includes failures without reporting them as successful scores', () => {
  const run = { counts: { candidates: 80, scored: 46, failed: 34, cached: 10, selected: 20, filtered: 26 } };
  assert.deepEqual(pipelineProgress(run), { total: 80, completed: 80, percent: 100 });
  assert.equal(pipelineCounts(run).scored, 46);
  assert.equal(pipelineCounts(run).selected, 20);
  assert.equal(pipelineCounts({ filtered_count: 26 }).selected, 0);
  assert.deepEqual(pipelineProgress({ progress: { total: 0, completed: 2 } }), { total: 0, completed: 0, percent: 0 });
});

test('score histogram excludes missing, failed and invalid scores, retaining exact boundaries', () => {
  const values = [null, undefined, NaN, -1, 101, 0, 19.9, 20, 39.9, 40, 60, 80, 100];
  assert.deepEqual(scoreDistribution(values.map((score) => ({ score }))).map((bin) => bin.count), [2, 2, 1, 1, 2]);
  const groups = sourceDistribution([{ source: 'A', status: 'failed' }, { source: 'A', status: 'selected' }, { source: 'B', status: 'scoring' }]);
  assert.equal(groups[0].failed, 1); assert.equal(groups[0].selected, 1); assert.equal(groups[1].pending, 1);
});

test('trace links allow HTTPS without credentials and duration handles absent data', () => {
  assert.equal(safeTraceUrl('javascript:alert(1)'), '');
  assert.equal(safeTraceUrl('https://user:password@smith.langchain.com'), '');
  assert.equal(safeTraceUrl('https://smith.langchain.com/runs/test'), 'https://smith.langchain.com/runs/test');
  assert.equal(durationLabel(undefined), '—'); assert.equal(durationLabel(65100), '1m 5s');
});

test('official and BAI Jev use distinct configured adapters; discovery uses persisted credentials', () => {
  const official = JEV_PROVIDER_PRESETS.find((provider) => provider.decisions_api === 'typesafe');
  assert.equal(official.api_base, 'https://api.typesafe.ai/v1');
  assert.equal(official.api_protocol, 'decisions');
  const saved = { ...official, provider_id: 'official', secrets: { api_key: { value: 'synthetic' } } };
  assert.equal(savedScorerConnection({ ...saved, model: 'jev-1.13.0' }, saved), true);
  assert.equal(savedScorerConnection({ ...saved, decisions_api: 'bai' }, saved), false);
  assert.equal(savedScorerConnection({ ...saved, secrets: { api_key: { value: 'new-synthetic' } } }, saved), false);
  assert.equal(savedScorerConnection(saved, undefined), false);
});
