export const historyNumber = (value, digits = 2) => value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits === 2 ? 2 : 0 });
export const historyDate = (value, zh) => value ? new Date(value).toLocaleString(zh ? 'zh-CN' : 'en-GB', { hour12: false }) : '—';
export const pnlClass = value => value == null || value === 0 ? '' : value < 0 ? 'is-negative' : 'is-positive';
