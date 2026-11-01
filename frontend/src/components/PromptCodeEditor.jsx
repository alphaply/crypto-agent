import React, { forwardRef, useImperativeHandle, useRef, useState } from 'react';
import { Button, Typography } from 'antd';
import { usePreferences } from '../app/usePreferences';

const PromptCodeEditor = forwardRef(function PromptCodeEditor({ value, onChange, placeholder, height = 420, onSave, disabled = false }, ref) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const textRef = useRef(null);
  const gutterRef = useRef(null);
  const [wrap, setWrap] = useState(true);
  const [cursor, setCursor] = useState(0);
  const safeValue = value || '';
  const lineCount = safeValue.split('\n').length;
  const prefix = safeValue.slice(0, cursor);
  const line = prefix.split('\n').length;
  const column = prefix.length - prefix.lastIndexOf('\n');

  useImperativeHandle(ref, () => ({
    insertText(text) {
      const textarea = textRef.current;
      if (!textarea || disabled) return;
      textarea.focus();
      // Native insertion keeps the browser's undo history where supported.
      if (!document.execCommand('insertText', false, text)) {
        textarea.setRangeText(text, textarea.selectionStart, textarea.selectionEnd, 'end');
        onChange(textarea.value);
      }
      setCursor(textarea.selectionEnd);
    },
  }), [disabled, onChange]);

  return (
    <div className="prompt-editor-wrapper">
      <div className="prompt-editor-toolbar">
        <Typography.Text type="secondary">{zh ? '纯文本编辑 · 变量可在下方插入' : 'Plain text · Insert variables below'}</Typography.Text>
        <Button size="small" aria-pressed={wrap} onClick={() => setWrap((current) => !current)}>{zh ? `自动换行：${wrap ? '开' : '关'}` : `Word wrap: ${wrap ? 'on' : 'off'}`}</Button>
      </div>
      <div className={`prompt-editor prompt-native-editor ${wrap ? 'is-wrapped' : ''}`} style={{ height }}>
        {!wrap ? <div className="prompt-editor__gutter" ref={gutterRef} aria-hidden="true"><pre className="prompt-editor__line-numbers">{Array.from({ length: lineCount }, (_, index) => index + 1).join('\n')}</pre></div> : null}
        <div className="prompt-editor__body">
          <textarea
            ref={textRef}
            className="prompt-editor__textarea"
            value={safeValue}
            onChange={(event) => onChange(event.target.value)}
            onSelect={(event) => setCursor(event.target.selectionStart)}
            onScroll={(event) => { if (gutterRef.current) gutterRef.current.scrollTop = event.target.scrollTop; }}
            placeholder={placeholder}
            spellCheck={false}
            wrap={wrap ? 'soft' : 'off'}
            aria-label={zh ? 'Prompt 编辑器' : 'Prompt editor'}
            disabled={disabled}
            onKeyDown={(event) => {
              if (onSave && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
                event.preventDefault();
                onSave();
              }
            }}
          />
        </div>
      </div>
      <div className="prompt-editor-meta" aria-live="off">
        <Typography.Text type="secondary">{lineCount} {zh ? '行' : 'lines'} · {safeValue.length} {zh ? '字符' : 'characters'}</Typography.Text>
        <Typography.Text type="secondary">{zh ? `光标：第 ${line} 行，第 ${column} 列` : `Ln ${line}, Col ${column}`}</Typography.Text>
      </div>
    </div>
  );
});

export default PromptCodeEditor;
