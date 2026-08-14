export function normalizeReasoningMarkdown(reasoning = '') {
  let normalized = String(reasoning || '')
    .replace(/\r\n?/g, '\n')
    .replace(/([^#\n])(?=#{1,6}\s*(?:推理(?:阶段|过程)|Reasoning\s+(?:Stage|Process)))/gi, '$1\n\n')
    .replace(/([^\n])\n(?=#{1,6}\s*(?:推理(?:阶段|过程)|Reasoning\s+(?:Stage|Process)))/gi, '$1\n\n')
    .replace(/^(#{1,6}\s*(?:推理(?:阶段|过程)|Reasoning\s+(?:Stage|Process))\s*\d+[^\n]*)\n(?!\n)/gim, '$1\n\n')
    .trim();

  const stageHeading = /^#{1,6}\s*(?:推理(?:阶段|过程)|Reasoning\s+(?:Stage|Process))\s*\d+[^\n]*$/gim;
  const matches = [...normalized.matchAll(stageHeading)];
  if (matches.length === 1 && matches[0].index === 0) {
    normalized = normalized.slice(matches[0][0].length).replace(/^\s*(?:---\s*)?/, '').trim();
  }
  return normalized;
}

export function splitThinkingContent(content = '', explicitReasoning = '') {
  const text = String(content || '');
  const extracted = [];
  const blockPattern = /<(think|thinking)\b[^>]*>([\s\S]*?)(?:<\/\1\s*>|$)/gi;
  const cleaned = text
    .replace(blockPattern, (_match, _tag, reasoning) => {
      const normalized = String(reasoning || '').trim();
      if (normalized) extracted.push(normalized);
      return '';
    })
    .replace(/<\/?(?:think|thinking)\b[^>]*>/gi, '')
    .trim();

  const explicit = normalizeReasoningMarkdown(explicitReasoning);
  return {
    content: cleaned,
    reasoning: explicit || normalizeReasoningMarkdown(extracted.join('\n\n')),
    thinkingOpen: /<(think|thinking)\b[^>]*>(?![\s\S]*<\/\1\s*>)/i.test(text),
  };
}
