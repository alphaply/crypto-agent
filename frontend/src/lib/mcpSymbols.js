export const MCP_SYMBOL_PAGE_SIZE = 50;

export function mcpProfileDraft(profile) {
  return {
    ...profile,
    symbol_scope: profile.symbol_scope === 'all' ? 'all' : 'selected',
    symbols: [...(profile.symbols || [])],
  };
}

export function mcpProfilePayload(profile) {
  const draft = mcpProfileDraft(profile);
  return { ...draft, symbols: draft.symbol_scope === 'all' ? [] : draft.symbols };
}

export function mcpSelectedQuote(value, marketType) {
  return marketType === 'spot' ? (value[0]?.split('/')[1]?.split(':')[0] || '') : '';
}

export function mcpSymbolRequest({ profileId, marketType, value, query, quote, page }) {
  return {
    exchange_profile_id: profileId,
    market_type: marketType,
    linear_only: marketType === 'swap' || undefined,
    keyword: query || undefined,
    quote: quote || undefined,
    symbols: value.join(',') || undefined,
    limit: MCP_SYMBOL_PAGE_SIZE,
    offset: page * MCP_SYMBOL_PAGE_SIZE,
  };
}

export function mcpSymbolOptions(records, value, marketType) {
  const limit = marketType === 'spot' ? 10 : 30;
  const quote = mcpSelectedQuote(value, marketType);
  return [...new Map(records.map((record) => [record.symbol, record])).values()].map((record) => ({
    value: record.symbol,
    label: record.display_name || record.symbol,
    disabled: !value.includes(record.symbol) && (value.length >= limit || Boolean(quote && record.quote !== quote)),
  }));
}

export function mcpSelectedSymbolLabels(result) {
  return new Map([...(result?.symbols || []), ...(result?.selected_symbols || [])].map((record) => [
    record.symbol, record.display_name || record.symbol,
  ]));
}
