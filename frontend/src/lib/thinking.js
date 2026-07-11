export function splitThinkingContent(content = '', explicitReasoning = '') {
  const text = String(content || '');
  const match = text.match(/<thinking>\s*([\s\S]*?)\s*<\/thinking>/i);
  if (!match) {
    return { content: text, reasoning: explicitReasoning || '' };
  }
  const cleaned = text.replace(match[0], '').trim();
  const reasoning = explicitReasoning || match[1].trim();
  return { content: cleaned, reasoning };
}
