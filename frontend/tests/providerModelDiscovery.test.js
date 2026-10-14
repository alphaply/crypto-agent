import test from 'node:test';
import assert from 'node:assert/strict';
import { createModelDiscovery, discoveryConnection, discoveryConnectionKey, discoveryIssue,
  modelMatches, normalizeDiscoveredModels } from '../src/lib/providerModelDiscovery.js';

const provider = { provider_id: 'saved', api_base: 'https://Example.com/Custom/v1', model: 'unlisted-alias',
  api_protocol: 'chat', compatibility_mode: 'auto', secrets: { api_key: { configured: true, value: 'fixture-secret' } } };
const result = (id = 'Exact/Model') => ({ success: true, models: [{ id, name: 'Friendly name' }],
  complete: true, fetched_at: '2026-10-13T00:00:00Z', source: { url: 'https://example.com/models' } });
const tick = async () => { await Promise.resolve(); await Promise.resolve(); };

function setup() {
  const scheduled = new Map();
  const pending = [];
  let clockId = 0;
  const control = createModelDiscovery({
    schedule(fn, ms) { assert.equal(ms, 300); scheduled.set(++clockId, fn); return clockId; },
    cancel(id) { scheduled.delete(id); },
    fetchModels(body, signal) {
      return new Promise((resolve, reject) => pending.push({ body, signal, resolve, reject }));
    },
  });
  return { control, scheduled, pending, flush() {
    for (const [id, fn] of scheduled) { scheduled.delete(id); fn(); }
  } };
}

test('identity preserves paths/ports and ignores non-connection fields without embedding secrets', () => {
  const identity = discoveryConnectionKey(provider);
  assert.equal(identity, discoveryConnectionKey({ ...provider, api_base: ' HTTPS://example.com/Custom/v1/ ', model: 'different', name: 'renamed' }));
  assert.notEqual(identity, discoveryConnectionKey({ ...provider, api_base: 'https://example.com/custom/v1' }));
  assert.notEqual(identity, discoveryConnectionKey({ ...provider, api_base: 'https://example.com:443/Custom/v1' }));
  assert.equal(discoveryConnectionKey({ api_base: 'https://user:pass@example.com' }), '');
  assert.equal(discoveryConnectionKey({ api_base: 'https://example.com/?secret=x' }), '');
  assert(!discoveryConnection(provider, provider).identity.includes('fixture-secret'));
});

test('saved, replaced, cleared, copied and changed-endpoint credentials are distinct', () => {
  assert.deepEqual(discoveryConnection(provider, provider).body.credential, { mode: 'saved', provider_id: 'saved' });
  const moved = { ...provider, api_base: 'https://other.example/v1' };
  assert.equal(discoveryConnection(moved, provider).ready, false);
  assert.equal(discoveryConnection({ ...provider, provider_id: 'copy' }, provider).ready, false);
  assert.equal(discoveryConnection(provider, undefined).ready, false);
  assert.equal(discoveryConnection(moved, provider, { edited: true, version: 1 }).body.credential.mode, 'draft');
  for (const secret of [{ clear: true, value: 'fixture-secret' }, { value: '' }]) {
    const next = { ...provider, secrets: { api_key: secret } };
    assert.equal(discoveryConnection(next, provider, { edited: true, version: 2 }).ready, false);
  }
});

test('normalization deduplicates exact IDs; search includes friendly names and manual aliases stay untouched', () => {
  const models = normalizeDiscoveredModels([{ id: 'B', name: 'Friendly' }, { id: 'b' }, { id: 'B' }, {}, null]);
  assert.deepEqual(models.map((row) => row.id), ['B', 'b']);
  assert(modelMatches('FRIEND', models[0]));
  assert(modelMatches('b', models[0]));
  assert(!modelMatches('unlisted', models[0]));
  assert.equal(provider.model, 'unlisted-alias');
  assert.equal(normalizeDiscoveredModels(null).length, 0);
});

test('only a committed edit schedules once; non-connection changes do not reload', async () => {
  const { control, flush, pending, scheduled } = setup();
  control.open(discoveryConnection(provider, undefined));
  for (let version = 1; version <= 5; version++) {
    control.update(discoveryConnection(provider, undefined, { edited: true, version }));
    flush();
  }
  assert.equal(pending.length, 0);
  control.commit(); control.commit();
  assert.equal(scheduled.size, 1);
  flush();
  assert.equal(pending.length, 1);
  pending[0].resolve(result()); await tick();
  assert.equal(control.getSnapshot().models[0].id, 'Exact/Model');
  assert.equal(provider.model, 'unlisted-alias'); // No default-selection callback exists.
  control.update(discoveryConnection({ ...provider, temperature: 1, model: 'typed-alias', input_price_per_m: 2 }, undefined, { edited: true, version: 5 }), true);
  control.commit(); flush();
  assert.equal(pending.length, 1);
  control.close();
});

for (const late of ['success', 'error']) {
  test(`late A ${late} and finally cannot change B loading or results`, async () => {
    const { control, flush, pending } = setup();
    control.open(discoveryConnection(provider, provider)); flush();
    const newer = { ...provider, api_base: 'https://second.example/v1' };
    control.update(discoveryConnection(newer, provider, { edited: true, version: 1 }), true); flush();
    assert(pending[0].signal.aborted);
    if (late === 'success') pending[0].resolve(result('old'));
    else pending[0].reject(new Error('secret-echo'));
    await tick();
    assert.equal(control.getSnapshot().loading, true);
    assert.equal(control.getSnapshot().error, null);
    assert.deepEqual(control.getSnapshot().models, []);
    pending[1].resolve(result('current')); await tick();
    assert.equal(control.getSnapshot().models[0].id, 'current');
    control.close();
  });
}

test('same connection refresh failure retains dated results as stale; changed connection clears them', async () => {
  const { control, flush, pending } = setup();
  control.open(discoveryConnection(provider, provider)); flush();
  pending[0].resolve(result()); await tick();
  void control.refresh();
  pending[1].reject({ response: { data: { detail: { code: 'rate_limited', message: 'secret-echo' } } }, config: { api_key: 'secret-echo' } });
  await tick();
  const state = control.getSnapshot();
  assert(state.stale && state.loaded && state.fetchedAt && !state.loading);
  assert.equal(state.models.length, 1);
  assert(!JSON.stringify(state).includes('secret-echo'));
  control.update(discoveryConnection({ ...provider, compatibility_mode: 'anthropic' }, provider));
  assert.deepEqual(control.getSnapshot().models, []);
  control.close();
});

test('closing cancels debounce and in-flight work; late response causes no state publication', async () => {
  const { control, flush, pending } = setup();
  control.open(discoveryConnection(provider, provider)); control.close(); flush();
  assert.equal(pending.length, 0);
  control.open(discoveryConnection(provider, provider)); flush();
  control.close();
  let updates = 0;
  control.subscribe(() => updates++);
  pending[0].resolve(result()); await tick();
  assert.equal(updates, 0);
  assert(pending[0].signal.aborted);
});

test('partial/empty states and structured errors support bilingual manual fallback', async () => {
  const { control, flush, pending } = setup();
  control.open(discoveryConnection(provider, provider)); flush();
  pending[0].resolve({ ...result(), models: [], complete: false, warning: { code: 'pagination_error' } });
  await tick();
  assert(control.getSnapshot().loaded && !control.getSnapshot().complete);
  assert.match(discoveryIssue({ code: 'catalog_not_supported' }), /手动/);
  assert.match(discoveryIssue({ response: { data: { detail: { code: 'timeout' } } } }, 'en'), /timed out/);
  assert(!discoveryIssue({ message: 'secret-echo' }).includes('secret-echo'));
  control.close();
});
