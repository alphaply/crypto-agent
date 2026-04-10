(function initAppTheme() {
  const MODE_KEY = 'crypto-agent-theme-mode';
  const DEFAULT_MODE = 'auto';
  const allowedModes = new Set(['light', 'dark', 'auto']);

  function getStoredMode() {
    const mode = localStorage.getItem(MODE_KEY) || DEFAULT_MODE;
    return allowedModes.has(mode) ? mode : DEFAULT_MODE;
  }

  function getSystemTheme() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function resolveTheme(mode) {
    return mode === 'auto' ? getSystemTheme() : mode;
  }

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
    const mode = getStoredMode();
    const resolved = resolveTheme(mode);

    document.querySelectorAll('[data-theme-mode-select]').forEach(select => {
      if (select.value !== mode) select.value = mode;
    });

    document.querySelectorAll('[data-theme-toggle]').forEach(button => {
      const label = mode === 'auto' ? `主题: 跟随系统(${resolved === 'dark' ? '暗' : '亮'})` : `主题: ${resolved === 'dark' ? '暗色' : '亮色'}`;
      button.textContent = label;
      button.setAttribute('aria-label', label);
    });
  }

  function toggleMode() {
    const mode = getStoredMode();
    const next = mode === 'light' ? 'dark' : (mode === 'dark' ? 'auto' : 'light');
    applyTheme(next, true);
  }

  function initThemeControls() {
    document.querySelectorAll('[data-theme-toggle]').forEach(button => {
      button.addEventListener('click', () => toggleMode());
    });

    document.querySelectorAll('[data-theme-mode-select]').forEach(select => {
      select.addEventListener('change', () => applyTheme(select.value, true));
    });

    updateThemeControls();
  }

  function init() {
    applyTheme(getStoredMode(), false);

    const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
    if (media && typeof media.addEventListener === 'function') {
      media.addEventListener('change', () => {
        if (getStoredMode() === 'auto') {
          applyTheme('auto', false);
        }
      });
    }

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
