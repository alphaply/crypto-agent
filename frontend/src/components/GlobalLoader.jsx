import { useEffect, useRef, useState } from 'react';

/**
 * 全局顶部加载进度条
 * 通过监听自定义事件 'global-loading-start' / 'global-loading-end' 控制显示
 */
export default function GlobalLoader() {
  const [visible, setVisible] = useState(false);
  const [progress, setProgress] = useState(0);
  const timerRef = useRef(null);
  const countRef = useRef(0);

  useEffect(() => {
    const onStart = () => {
      countRef.current += 1;
      if (countRef.current === 1) {
        clearInterval(timerRef.current);
        setProgress(0);
        setVisible(true);
        // 假进度：快速到80%，然后缓慢推进
        let p = 0;
        timerRef.current = setInterval(() => {
          p += p < 70 ? 8 : p < 90 ? 2 : 0.5;
          if (p >= 92) {
            clearInterval(timerRef.current);
            p = 92;
          }
          setProgress(p);
        }, 120);
      }
    };

    const onEnd = () => {
      countRef.current = Math.max(0, countRef.current - 1);
      if (countRef.current === 0) {
        clearInterval(timerRef.current);
        setProgress(100);
        // 短暂停留后隐藏
        setTimeout(() => {
          setVisible(false);
          setProgress(0);
        }, 320);
      }
    };

    window.addEventListener('global-loading-start', onStart);
    window.addEventListener('global-loading-end', onEnd);
    return () => {
      window.removeEventListener('global-loading-start', onStart);
      window.removeEventListener('global-loading-end', onEnd);
      clearInterval(timerRef.current);
    };
  }, []);

  if (!visible) return null;

  return (
    <div
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        height: 3,
        zIndex: 9999,
        pointerEvents: 'none',
        background: 'transparent',
      }}
    >
      <div
        style={{
          height: '100%',
          width: `${progress}%`,
          background: 'var(--accent, #2563eb)',
          transition: progress === 100 ? 'width 0.15s ease' : 'width 0.12s linear',
          borderRadius: '0 2px 2px 0',
          boxShadow: '0 0 8px var(--accent, #2563eb)',
        }}
      />
    </div>
  );
}
