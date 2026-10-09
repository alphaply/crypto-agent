import { useEffect, useState } from 'react';
import { Alert, Button, Empty, Select, Space, Spin, Typography } from 'antd';
import { api } from '../lib/api';
import { MCP_SYMBOL_PAGE_SIZE, mcpSelectedQuote, mcpSelectedSymbolLabels, mcpSymbolOptions, mcpSymbolRequest } from '../lib/mcpSymbols';

// The parent keys this picker by account and market, and unmounts it when the
// editor closes. Late responses cannot populate another account or modal.
export default function McpSymbolPicker({ profileId, marketType, value, onChange, locale = 'zh' }) {
  const zh = locale === 'zh';
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [quoteFilter, setQuoteFilter] = useState('');
  const [page, setPage] = useState(0);
  const [retry, setRetry] = useState(0);
  const [result, setResult] = useState(null);
  const [failure, setFailure] = useState(null);
  const symbolsKey = value.join(',');
  const selectedQuote = mcpSelectedQuote(value, marketType);
  const quote = selectedQuote || quoteFilter;
  const limit = marketType === 'spot' ? 10 : 30;
  const requestKey = JSON.stringify([profileId, marketType, symbolsKey, query, quote, page, retry]);
  const currentResult = result?.requestKey === requestKey ? result : null;
  const error = failure?.requestKey === requestKey ? failure.message : '';
  const searching = search.trim() !== query;
  const loading = Boolean(profileId && (!currentResult && !error || searching));
  const labels = mcpSelectedSymbolLabels(result);
  const invalid = currentResult?.invalid_symbols || [];

  useEffect(() => {
    const timer = window.setTimeout(() => { setQuery(search.trim()); setPage(0); }, 250);
    return () => window.clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    if (!profileId) return undefined;
    let active = true;
    const controller = new AbortController();
    api.get('/config/market-symbols', {
      params: mcpSymbolRequest({ profileId, marketType, value: symbolsKey ? symbolsKey.split(',') : [], query, quote, page }),
      signal: controller.signal,
      silent: true,
    }).then(({ data }) => {
      if (active) setResult({ ...data, requestKey });
    }).catch((err) => {
      if (active && err.code !== 'ERR_CANCELED') setFailure({ requestKey, message: err.message || (zh ? '交易所标的加载失败' : 'Unable to load exchange markets') });
    });
    return () => { active = false; controller.abort(); };
  }, [profileId, marketType, symbolsKey, query, quote, page, retry, requestKey, zh]);

  return <Space direction="vertical" size="small" style={{ width: '100%', minWidth: 0 }}>
    {!profileId && <Typography.Text type="secondary">{zh ? '先选择交易账户，再搜索该市场的可交易标的。' : 'Choose an exchange account before searching its tradable markets.'}</Typography.Text>}
    {profileId && <Space wrap size="small">
      <Typography.Text type="secondary">{zh ? '计价币' : 'Quote currency'}</Typography.Text>
      <Select
        aria-label={zh ? 'MCP 标的计价币' : 'MCP quote currency'}
        value={quote || undefined}
        placeholder={zh ? '全部' : 'All'}
        style={{ minWidth: 115 }}
        allowClear={!selectedQuote}
        disabled={Boolean(selectedQuote)}
        options={[...new Set([quote, ...(currentResult?.quote_currencies || result?.quote_currencies || [])].filter(Boolean))].map((item) => ({ value: item, label: item }))}
        onChange={(next) => { setQuoteFilter(next || ''); setPage(0); }}
      />
      <Typography.Text type="secondary">{value.length} / {limit}</Typography.Text>
    </Space>}
    <Select
      mode="multiple"
      aria-label={zh ? 'MCP 允许交易的标的' : 'MCP allowed symbols'}
      value={value}
      labelRender={({ value: symbol }) => labels.get(symbol) || symbol}
      disabled={!profileId}
      showSearch
      filterOption={false}
      searchValue={search}
      onSearch={(next) => setSearch(next.slice(0, 100))}
      autoClearSearchValue={false}
      loading={loading}
      options={searching ? [] : mcpSymbolOptions(currentResult?.symbols || [], value, marketType)}
      placeholder={zh ? '搜索并多选，例如 BTC、ETH' : 'Search and select markets, e.g. BTC or ETH'}
      status={invalid.length ? 'warning' : undefined}
      onChange={(next) => {
        if (next.length <= limit) { onChange(next); setSearch(''); setQuery(''); setPage(0); }
      }}
      style={{ width: '100%' }}
      notFoundContent={loading ? <Spin size="small" /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={zh ? '没有匹配的可交易标的' : 'No matching tradable markets'} />}
    />
    <Typography.Text type="secondary">{marketType === 'spot'
      ? (zh ? '从交易所目录选择 1–10 个相同计价币的现货标的，共享每次调用的额度。' : 'Select 1–10 spot markets with the same quote currency, sharing one allowance per call.')
      : (zh ? '从交易所目录选择 1–30 个线性永续合约标的。' : 'Select 1–30 linear perpetual markets from the exchange catalog.')}</Typography.Text>
    {error && <Alert type="error" showIcon title={zh ? '标的加载失败，已有选择已保留' : 'Market lookup failed; your selection is preserved'} description={error} action={<Button size="small" onClick={() => setRetry((current) => current + 1)}>{zh ? '重试' : 'Retry'}</Button>} />}
    {invalid.length > 0 && <Alert type="warning" showIcon title={zh ? `以下已选名称未匹配当前目录，请核对或重新选择：${invalid.join('、')}` : `These selected names were not found in the current catalog. Check or reselect them: ${invalid.join(', ')}`} />}
    {currentResult && (currentResult.total > MCP_SYMBOL_PAGE_SIZE || page > 0) && <Space wrap>
      <Button size="small" disabled={page === 0 || loading} onClick={() => setPage((current) => current - 1)}>{zh ? '上一页' : 'Previous'}</Button>
      <Typography.Text type="secondary">{zh ? `第 ${page + 1} 页，共 ${currentResult.total} 个标的` : `Page ${page + 1} · ${currentResult.total} markets`}</Typography.Text>
      <Button size="small" disabled={!currentResult.has_more || loading} onClick={() => setPage((current) => current + 1)}>{zh ? '下一页' : 'Next'}</Button>
    </Space>}
  </Space>;
}
