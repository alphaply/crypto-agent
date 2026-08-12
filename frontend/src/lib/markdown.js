import remend from 'remend';

const STREAMING_REPAIR_OPTIONS = {
  // Currency values are common in this project, so avoid treating `$` as math.
  inlineKatex: false,
  katex: false,
};

export function prepareMarkdown(content = '', streaming = false) {
  const source = String(content || '').replace(/\r\n?/g, '\n');
  return streaming ? remend(source, STREAMING_REPAIR_OPTIONS) : source;
}

export function isCompactPlainCode(value = '', className = '') {
  const source = String(value || '').replace(/\n$/, '').trim();
  if (!source || /(?:^|\s)language-[\w-]+/.test(String(className || ''))) return false;
  return !source.includes('\n') && source.length <= 96;
}
