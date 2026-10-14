import assert from 'node:assert/strict';
import test from 'node:test';
import { managementDraft, managementError, managementLabel, observationTime, sourceLabel } from '../src/lib/positionManagement.js';
import { mcpProfilePayload } from '../src/lib/mcpSymbols.js';

test('new and legacy edit drafts use automatic management without mutating saved scopes', () => {
  assert.equal(managementDraft({}).position_management_policy, 'visible_first_claim');
  const original = { position_management_policy: 'exclusive_scope', management_scopes: [{ symbol: 'ETH/USDT:USDT', side: 'LONG' }] };
  const draft = managementDraft(original);
  assert.equal(draft.position_management_policy, 'visible_first_claim');
  assert.deepEqual(draft.management_scopes, []);
  assert.equal(original.management_scopes[0].side, 'LONG');
});

test('automatic all-symbol access has no extra finite direction list', () => {
  const payload = mcpProfilePayload({ symbol_scope: 'all', symbols: ['ETH/USDT:USDT'], position_management_policy: 'exclusive_scope', management_scopes: [{ symbol: 'ETH/USDT:USDT', side: 'LONG' }] });
  assert.deepEqual(payload.symbols, []);
  assert.equal(payload.position_management_policy, 'visible_first_claim');
  assert.deepEqual(payload.management_scopes, []);
  assert.equal(managementError(payload, { supported: false }), '');
});

test('resource origin does not imply management permission or ownership', () => {
  assert.equal(managementLabel({ source: 'external', can_claim: true }), '可自动认领');
  assert.equal(managementLabel({ source: 'external', managed: true }, 'en'), 'Managed');
  assert.equal(managementLabel({ management_state: 'conflict' }), '其他管理者占用');
  assert.equal(managementLabel({ source: 'external' }), '待核验');
});

test('exclusive validation rejects unsupported modes, invalid direction, duplicate or out-of-scope symbols', () => {
  const config = { position_management_policy: 'exclusive_scope', management_scopes: [{ symbol: 'ETH/USDT:USDT', side: 'LONG' }] };
  assert.equal(managementError(config, { symbols: ['ETH/USDT:USDT'] }), '');
  assert.ok(managementError(config, { supported: false }));
  assert.ok(managementError(config, { symbols: ['BTC/USDT:USDT'] }));
  assert.ok(managementError({ ...config, management_scopes: [...config.management_scopes, ...config.management_scopes] }));
  assert.ok(managementError({ ...config, management_scopes: [{ symbol: 'ETH/USDT:USDT', side: 'BOTH' }] }));
  assert.equal(managementError({ position_management_policy: 'strict_owned' }, { supported: false }), '');
});

test('missing observations do not become current timestamps or human attribution', () => {
  assert.equal(observationTime(null, 'en'), 'Not verified');
  assert.equal(sourceLabel('external', 'en'), 'External');
  assert.equal(sourceLabel(null, 'en'), 'Source unverified');
});
