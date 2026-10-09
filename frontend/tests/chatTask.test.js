import test from 'node:test';
import assert from 'node:assert/strict';
import { buildInitialRuntime, buildChatTaskPayload, readLastRuntime, saveLastRuntime } from '../src/lib/chatTask.js';

const bootstrap = {
  exchange_profiles: [
    { profile_id: 'unset', configured: false },
    { profile_id: 'account', configured: true, supported_market_types: ['spot', 'swap'] },
  ],
  llm_providers: [
    { provider_id: 'unset', api_key_configured: false },
    { provider_id: 'model', api_key_configured: true, system_prompt_role: 'user' },
  ],
  configs: [{ config_id: 'trading-task' }],
};

test('a new task defaults to analysis even when trading configurations exist', () => {
  const runtime = buildInitialRuntime(bootstrap, {});
  assert.equal(runtime.symbol, '');
  assert.equal(runtime.exchange_profile_id, 'account');
  assert.equal(runtime.llm_provider_id, 'model');
  assert.equal(runtime.system_prompt_role, 'user');
  const payload = buildChatTaskPayload({ ...runtime, read_only: false });
  assert.equal(payload.mode, 'temporary');
  assert.equal(payload.runtime.read_only, true);
  assert.equal(payload.config_id, undefined);
});

test('an explicit association enables the existing task approval flow; clearing restores analysis', () => {
  const runtime = buildInitialRuntime(bootstrap, {});
  assert.deepEqual(buildChatTaskPayload(runtime, 'trading-task'), { mode: 'task', config_id: 'trading-task' });
  assert.equal(buildChatTaskPayload(runtime, '').runtime.read_only, true);
});

test('new tasks retain the last symbol and market without retaining resolved secrets', () => {
  let stored;
  const storage = { setItem: (_, value) => { stored = value; }, getItem: () => stored };
  saveLastRuntime({ ...buildInitialRuntime(bootstrap, {}), symbol: 'ETH/USDT', market_type: 'swap', api_key: 'do-not-persist' }, storage);
  const initial = buildInitialRuntime(bootstrap, readLastRuntime(storage));
  assert.equal(initial.symbol, 'ETH/USDT');
  assert.equal(initial.market_type, 'swap');
  assert.equal(JSON.parse(stored).api_key, undefined);
  assert.deepEqual(buildInitialRuntime(bootstrap, initial), initial);
});

test('a removed market account is replaced with configured defaults', () => {
  const initial = buildInitialRuntime(bootstrap, { exchange_profile_id: 'deleted', symbol: 'OLD/USDT', market_type: 'future', llm_provider_id: 'deleted' });
  assert.equal(initial.exchange_profile_id, 'account');
  assert.equal(initial.market_type, 'spot');
  assert.equal(initial.symbol, '');
  assert.equal(initial.llm_provider_id, 'model');
});

test('blocked or corrupt browser storage cannot break task creation', () => {
  const blocked = { getItem: () => { throw new Error('blocked'); }, setItem: () => { throw new Error('blocked'); } };
  assert.deepEqual(readLastRuntime(blocked), {});
  assert.deepEqual(readLastRuntime({ getItem: () => 'not json' }), {});
  assert.deepEqual(readLastRuntime({ getItem: () => '[]' }), {});
  assert.doesNotThrow(() => saveLastRuntime(buildInitialRuntime(bootstrap, {}), blocked));
});
