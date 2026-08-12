import React, { useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { isCompactPlainCode, prepareMarkdown } from '../lib/markdown';

function childText(children) {
  return React.Children.toArray(children).map((child) => {
    if (typeof child === 'string' || typeof child === 'number') return String(child);
    return React.isValidElement(child) ? childText(child.props.children) : '';
  }).join('');
}

const markdownComponents = {
  a: ({ node, ...props }) => {
    void node;
    return <a {...props} target="_blank" rel="noreferrer" />;
  },
  del: ({ children }) => <span>{children}</span>,
  table: ({ node, children, ...props }) => {
    void node;
    return (
      <div className="markdown-table-wrap" role="region" aria-label="Scrollable table" tabIndex={0}>
        <table {...props}>{children}</table>
      </div>
    );
  },
  pre: ({ node, children, ...props }) => {
    void node;
    const codeElement = React.Children.toArray(children).find(React.isValidElement);
    const codeClassName = codeElement?.props?.className || '';
    const value = childText(codeElement?.props?.children ?? children).replace(/\n$/, '');

    if (isCompactPlainCode(value, codeClassName)) {
      return (
        <span className="markdown-code-value">
          <code>{value}</code>
        </span>
      );
    }

    const languageClass = /(?:^|\s)language-[\w-]+/.test(codeClassName)
      ? 'markdown-code--language'
      : 'markdown-code--plain';
    return <pre {...props} className={`markdown-code ${languageClass}`}>{children}</pre>;
  },
  code: ({ node, className: codeClassName, children, ...props }) => {
    void node;
    return <code className={codeClassName} {...props}>{children}</code>;
  },
};

export default function MarkdownBlock({ content, className = '', streaming = false }) {
  const source = useMemo(() => prepareMarkdown(content, streaming), [content, streaming]);

  if (!source.trim()) {
    return null;
  }

  return (
    <div className={`markdown-block ${className}`.trim()}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={markdownComponents}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}
