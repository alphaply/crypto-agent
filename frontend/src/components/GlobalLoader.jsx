import { useEffect, useRef, useState } from 'react';

/**
 * 全局顶部加载进度条
 * 通过监听自定义事件 'global-loading-start' / 'global-loading-end' 控制显示
 */
export default function GlobalLoader() {
  const [visible, setVisible] = useState(false);
  const [progress, setProgress] = useState(0);
  const timerRef = useRef(null);
  const hideTimerRef = useRef(null);
  const countRef = useRef(0);

  useEffect(() => {
    const onStart = () => {
      countRef.current += 1;
      if (countRef.current === 1) {
        clearInterval(timerRef.current);
        clearTimeout(hideTimerRef.current);
        setProgress(12);
        setVisible(true);
        // 假进度：快速到 80%，然后缓慢推进
        let p = 12;
        timerRef.current = setInterval(() => {
          p += p < 70 ? 8 : p < 90 ? 2 : 0.45;
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
        clearTimeout(hideTimerRef.current);
        hideTimerRef.current = setTimeout(() => {
          setVisible(false);
          setProgress(0);
        }, 280);
      }
    };

    window.addEventListener('global-loading-start', onStart);
    window.addEventListener('global-loading-end', onEnd);
    return () => {
      window.removeEventListener('global-loading-start', onStart);
      window.removeEventListener('global-loading-end', onEnd);
      clearInterval(timerRef.current);
      clearTimeout(hideTimerRef.current);
    };
  }, []);

  if (!visible) return null;

  return (
    <div
      className={`global-loader${progress === 100 ? ' is-finishing' : ''}`}
      role="progressbar"
      aria-label="Loading"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(progress)}
    >
      <div
        className="global-loader__bar"
        style={{ width: `${progress}%` }}
      >
        <span className="global-loader__glow" />
      </div>
    </div>
  );
}
