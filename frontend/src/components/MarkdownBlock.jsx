import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export default function MarkdownBlock({ content, className = '' }) {
  if (!content?.trim()) {
    return null;
  }

  return (
    <div className={`markdown-block ${className}`.trim()}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
          code: ({ inline, className: codeClassName, children, ...props }) =>
            inline ? (
              <code className={codeClassName} {...props}>
                {children}
              </code>
            ) : (
              <pre className="markdown-code">
                <code className={codeClassName} {...props}>
                  {children}
                </code>
              </pre>
            ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
