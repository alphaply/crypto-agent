// Decode full SSE frames, including CRLF boundaries split across network chunks.
export function createSseParser(onEvent) {
  let buffer = '';
  return (chunk, final = false) => {
    buffer += chunk;
    buffer = buffer.replace(/\r\n/g, '\n');
    if (final && buffer.trim()) buffer += '\n\n';
    let boundary;
    while ((boundary = buffer.indexOf('\n\n')) >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const data = frame.split('\n').filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).replace(/^ /, '')).join('\n');
      if (data && data !== '[DONE]') onEvent(JSON.parse(data));
    }
  };
}
