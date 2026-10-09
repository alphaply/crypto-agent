import test from 'node:test';
import assert from 'node:assert/strict';
import { MCP_SYMBOL_PAGE_SIZE, mcpProfileDraft, mcpProfilePayload, mcpSelectedQuote, mcpSelectedSymbolLabels, mcpSymbolOptions, mcpSymbolRequest } from '../src/lib/mcpSymbols.js';

test('legacy profiles stay selected-only and editing never mutates the saved selection', () => {
  const original = { profile_id: 'legacy', symbols: ['BTC/USDT'] };
  const draft = mcpProfileDraft(original);
  assert.equal(draft.symbol_scope, 'selected');
  draft.symbols.push('ETH/USDT');
  assert.deepEqual(original.symbols, ['BTC/USDT']);
  assert.deepEqual(mcpProfilePayload(original).symbols, ['BTC/USDT']);
  assert.equal(mcpProfilePayload({ symbols: [] }).symbol_scope, 'selected');
});

test('unrestricted is explicit and sends no stale selected symbols while preserving the edit draft', () => {
  const draft = mcpProfileDraft({ symbol_scope: 'all', symbols: ['BTC/USDT'] });
  assert.equal(draft.symbol_scope, 'all');
  assert.deepEqual(mcpProfilePayload(draft), { symbol_scope: 'all', symbols: [] });
  assert.deepEqual(draft.symbols, ['BTC/USDT']);
  draft.symbol_scope = 'selected';
  assert.deepEqual(mcpProfilePayload(draft).symbols, ['BTC/USDT']);
  assert.equal(mcpProfileDraft({ symbol_scope: 'all', symbols: [] }).symbol_scope, 'all');
});

test('catalog requests carry the exact account, market, server search, selected symbols and page', () => {
  const params = mcpSymbolRequest({ profileId: 'okx-swap', marketType: 'swap', value: ['BTC/USDT:USDT'], query: 'ETH', quote: 'USDT', page: 2 });
  assert.deepEqual(params, {
    exchange_profile_id: 'okx-swap', market_type: 'swap', linear_only: true, keyword: 'ETH', quote: 'USDT',
    symbols: 'BTC/USDT:USDT', limit: MCP_SYMBOL_PAGE_SIZE, offset: 2 * MCP_SYMBOL_PAGE_SIZE,
  });
  const empty = mcpSymbolRequest({ profileId: 'binance-spot', marketType: 'spot', value: [], query: '', quote: '', page: 0 });
  assert.equal(empty.symbols, undefined);
  assert.equal(empty.keyword, undefined);
  assert.equal(empty.quote, undefined);
  assert.equal(empty.linear_only, undefined);
  assert.equal(empty.exchange_profile_id, 'binance-spot');
});

const records = [
  { symbol: 'BTC/USDT', quote: 'USDT', display_name: 'BTC/USDT · SPOT' },
  { symbol: 'ETH/USDT', quote: 'USDT', display_name: 'ETH/USDT · SPOT' },
  { symbol: 'ETH/BTC', quote: 'BTC', display_name: 'ETH/BTC · SPOT' },
];

test('spot options share a quote currency, cap at ten, and selected entries stay removable', () => {
  const options = mcpSymbolOptions(records, ['BTC/USDT'], 'spot');
  assert.equal(mcpSelectedQuote(['BTC/USDT'], 'spot'), 'USDT');
  assert.deepEqual(options.map((option) => option.disabled), [false, false, true]);
  const full = mcpSymbolOptions(records, ['BTC/USDT', ...Array.from({ length: 9 }, (_, index) => `ASSET${index}/USDT`)], 'spot');
  assert.deepEqual(full.map((option) => option.disabled), [false, true, true]);
  assert.equal(full[0].label, 'BTC/USDT · SPOT');
});

test('perpetual selections permit mixed quotes and cap at thirty', () => {
  assert.equal(mcpSelectedQuote(['BTC/USDT:USDT'], 'swap'), '');
  assert.deepEqual(mcpSymbolOptions(records, ['BTC/USDT'], 'swap').map((option) => option.disabled), [false, false, false]);
  const full = ['BTC/USDT', ...Array.from({ length: 29 }, (_, index) => `ASSET${index}/USDT:USDT`)];
  assert.deepEqual(mcpSymbolOptions(records, full, 'swap').map((option) => option.disabled), [false, true, true]);
});

test('server selected records retain labels outside the search result page and invalid legacy values remain available as raw tags', () => {
  const labels = mcpSelectedSymbolLabels({ symbols: [records[1]], selected_symbols: [records[0]], invalid_symbols: ['LEGACY/USDT'] });
  assert.equal(labels.get('BTC/USDT'), 'BTC/USDT · SPOT');
  assert.equal(labels.get('ETH/USDT'), 'ETH/USDT · SPOT');
  assert.equal(labels.get('LEGACY/USDT') || 'LEGACY/USDT', 'LEGACY/USDT');
  assert.equal(mcpSelectedSymbolLabels(null).size, 0);
});
