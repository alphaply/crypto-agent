export const RUNTIME_STORAGE_KEY = 'crypto-agent-chat-runtime-v1';

export function readLastRuntime(storage) {
  try {
    const target = storage || globalThis.localStorage;
    const value = JSON.parse(target?.getItem(RUNTIME_STORAGE_KEY) || '{}');
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  } catch {
    return {};
  }
}

export function buildInitialRuntime(data, last = readLastRuntime()) {
  const profiles = (data?.exchange_profiles || []).filter((item) => item.configured);
  const providers = (data?.llm_providers || []).filter((item) => item.api_key_configured);
  const profile = profiles.find((item) => item.profile_id === last.exchange_profile_id) || profiles[0] || {};
  const provider = providers.find((item) => item.provider_id === last.llm_provider_id) || providers[0] || {};
  const marketType = profile.supported_market_types?.includes(last.market_type)
    ? last.market_type : (profile.supported_market_types?.[0] || 'spot');
  const sameMarket = (!last.exchange_profile_id || last.exchange_profile_id === profile.profile_id)
    && (!last.market_type || last.market_type === marketType);
  return {
    exchange_profile_id: profile.profile_id || '',
    market_type: marketType,
    symbol: sameMarket && String(last.symbol || '').trim() ? last.symbol : '',
    llm_provider_id: provider.provider_id || '',
    temperature: last.temperature ?? null,
    global_requirement: last.global_requirement || '分析趋势、关键价位、多空证据和失效条件。优先说明数据质量与风险，不确定时明确说明。',
    system_prompt_role: last.system_prompt_role || provider.system_prompt_role || 'system',
  };
}

export function buildChatTaskPayload(runtime, associatedConfigId = '') {
  return associatedConfigId
    ? { mode: 'task', config_id: associatedConfigId }
    : { mode: 'temporary', runtime: { ...runtime, read_only: true } };
}

export function saveLastRuntime(runtime, storage) {
  // Only store UI preferences and profile references, never resolved credentials.
  const { exchange_profile_id, market_type, symbol, llm_provider_id, temperature, global_requirement, system_prompt_role } = runtime;
  try {
    const target = storage || globalThis.localStorage;
    target?.setItem(RUNTIME_STORAGE_KEY, JSON.stringify({ exchange_profile_id, market_type, symbol, llm_provider_id, temperature, global_requirement, system_prompt_role }));
  } catch {
    // A disabled browser store must not hide an already-created task.
  }
}
