(function initAppTheme() {
  const MODE_KEY = 'crypto-agent-theme-mode';
  const ACCENT_KEY = 'crypto-agent-theme-accent';
  const DEFAULT_MODE = 'auto';
  const DEFAULT_ACCENT = 'blue';
  const allowedModes = new Set(['light', 'dark', 'auto']);
  const allowedAccents = new Set(['blue', 'emerald', 'amber', 'rose', 'indigo', 'cyan']);

  function getStoredMode() {
    const mode = localStorage.getItem(MODE_KEY) || DEFAULT_MODE;
    return allowedModes.has(mode) ? mode : DEFAULT_MODE;
  }

  function getStoredAccent() {
    const accent = localStorage.getItem(ACCENT_KEY) || DEFAULT_ACCENT;
    return allowedAccents.has(accent) ? accent : DEFAULT_ACCENT;
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
        theme: resolved,
        accent: getStoredAccent()
      }
    }));

    updateThemeControls();
  }

  function applyAccent(accent, persist) {
    const nextAccent = allowedAccents.has(accent) ? accent : DEFAULT_ACCENT;
    document.documentElement.setAttribute('data-accent', nextAccent);
    if (persist) {
      localStorage.setItem(ACCENT_KEY, nextAccent);
    }

    window.dispatchEvent(new CustomEvent('app-theme-accent-change', {
      detail: {
        mode: getStoredMode(),
        theme: resolveTheme(getStoredMode()),
        accent: nextAccent
      }
    }));

    updateThemeControls();
  }

  function updateThemeControls() {
    const mode = getStoredMode();
    const accent = getStoredAccent();
    const resolved = resolveTheme(mode);

    document.querySelectorAll('[data-theme-mode-select]').forEach(select => {
      if (select.value !== mode) select.value = mode;
    });

    document.querySelectorAll('[data-theme-accent-select]').forEach(select => {
      if (select.value !== accent) select.value = accent;
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

    document.querySelectorAll('[data-theme-accent-select]').forEach(select => {
      select.addEventListener('change', () => applyAccent(select.value, true));
    });

    updateThemeControls();
  }

  function init() {
    applyAccent(getStoredAccent(), false);
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
    applyAccent,
    toggleMode,
    getMode: getStoredMode,
    getAccent: getStoredAccent,
    getResolvedTheme: () => resolveTheme(getStoredMode())
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
