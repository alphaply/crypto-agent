export const JEV_PROVIDER_PRESETS = [
  { key: 'typesafe-jev', label: 'TypeSafe · Jev 官方', api_base: 'https://api.typesafe.ai/v1', model: 'jev-latest', compatibility_mode: 'openai', api_protocol: 'decisions', decisions_api: 'typesafe', thinking_enabled: false },
  { key: 'bai-jev', label: 'BAI · Jev', api_base: 'https://api.b.ai/v1', model: 'jev-1.13.0', compatibility_mode: 'openai', api_protocol: 'decisions', decisions_api: 'bai', thinking_enabled: false },
];

export function savedScorerConnection(value, saved) {
  if (!saved || value.api_protocol !== 'decisions' || saved.api_protocol !== 'decisions') return false;
  const normalize = (url) => String(url || '').trim().replace(/\/+$/, '');
  return value.provider_id === saved.provider_id
    && (value.decisions_api || 'bai') === (saved.decisions_api || 'bai')
    && normalize(value.api_base) === normalize(saved.api_base)
    && Boolean(value.secrets?.api_key?.clear) === Boolean(saved.secrets?.api_key?.clear)
    && (value.secrets?.api_key?.value || '') === (saved.secrets?.api_key?.value || '');
}
