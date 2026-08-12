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
          del: ({ children }) => <span>{children}</span>,
          table: ({ children, ...props }) => (
            <div className="markdown-table-wrap" role="region" aria-label="Scrollable table" tabIndex={0}>
              <table {...props}>{children}</table>
            </div>
          ),
          // react-markdown v10 no longer supplies the legacy `inline` prop.
          // Block code is already wrapped in <pre>; bare <code> stays inline.
          pre: ({ children, ...props }) => (
            <pre className="markdown-code" {...props}>{children}</pre>
          ),
          code: ({ className: codeClassName, children, ...props }) => (
            <code className={codeClassName} {...props}>{children}</code>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
