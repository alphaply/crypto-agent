(function initAppTheme() {
  const MODE_KEY = 'crypto-agent-theme-mode';
  const DEFAULT_MODE = 'light';
  const allowedModes = new Set(['light', 'dark']);

  function getStoredMode() {
    const mode = localStorage.getItem(MODE_KEY);
    if (mode === 'auto') {
      return getSystemTheme();
    }
    return allowedModes.has(mode) ? mode : DEFAULT_MODE;
  }

  function getSystemTheme() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function resolveTheme(mode) { return mode; }

  function applyTheme(mode, persist) {
    const nextMode = allowedModes.has(mode) ? mode : DEFAULT_MODE;
    const resolved = resolveTheme(nextMode);
    const root = document.documentElement;
    root.setAttribute('data-theme-mode', nextMode);
    root.setAttribute('data-theme', resolved);

    if (persist) {
      localStorage.setItem(MODE_KEY, nextMode);
    }

    window.dispatchEvent(new CustomEvent('app-theme-change', {
      detail: {
        mode: nextMode,
        theme: resolved
      }
    }));

    updateThemeControls();
  }

  function updateThemeControls() {
    const resolved = resolveTheme(getStoredMode());
    const icon = resolved === 'dark'
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="16" height="16" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3a7 7 0 0 0 9.79 9.79z"></path></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="16" height="16" aria-hidden="true"><circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line></svg>';

    document.querySelectorAll('[data-theme-toggle]').forEach(button => {
      const label = resolved === 'dark' ? '切换到亮色模式' : '切换到暗色模式';
      button.innerHTML = icon;
      button.setAttribute('aria-label', label);
      button.setAttribute('title', label);
    });
  }

  function toggleMode() {
    const mode = getStoredMode();
    const next = mode === 'dark' ? 'light' : 'dark';
    applyTheme(next, true);
  }

  function initThemeControls() {
    document.querySelectorAll('[data-theme-toggle]').forEach(button => {
      button.addEventListener('click', () => toggleMode());
    });

    updateThemeControls();
  }

  function init() {
    applyTheme(getStoredMode(), false);

    initThemeControls();
  }

  window.AppTheme = {
    init,
    applyTheme,
    toggleMode,
    getMode: getStoredMode,
    getResolvedTheme: () => resolveTheme(getStoredMode())
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
