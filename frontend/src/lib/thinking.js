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

  const explicit = String(explicitReasoning || '').trim();
  return {
    content: cleaned,
    reasoning: explicit || extracted.join('\n\n'),
    thinkingOpen: /<(think|thinking)\b[^>]*>(?![\s\S]*<\/\1\s*>)/i.test(text),
  };
}
