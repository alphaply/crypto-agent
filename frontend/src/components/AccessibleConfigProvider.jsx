import { useMemo, useState } from 'react';
import { ConfigProvider, theme as antdTheme } from 'antd';

const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)';

function detectDarkAlgorithm(themeConfig) {
  const alg = themeConfig?.algorithm;
  if (!alg) return false;
  if (Array.isArray(alg)) {
    return alg.includes(antdTheme.darkAlgorithm);
  }
  return alg === antdTheme.darkAlgorithm;
}

function buildDesignTokens(isDark) {
  if (isDark) {
    return {
      token: {
        colorPrimary: '#3b82f6',
        colorInfo: '#38bdf8',
        colorSuccess: '#22c55e',
        colorWarning: '#fbbf24',
        colorError: '#f87171',
        colorBgBase: '#090d16',
        colorBgLayout: '#090d16',
        colorBgContainer: '#101726',
        colorBgElevated: '#162033',
        colorBgSpotlight: '#1e2c45',
        colorFillAlter: '#141e30',
        colorFillSecondary: '#172236',
        colorFillTertiary: '#121b2b',
        colorBorder: '#24344f',
        colorBorderSecondary: '#1e2c44',
        colorText: '#f1f5f9',
        colorTextSecondary: '#b6c4d8',
        colorTextTertiary: '#7c8fa9',
        colorTextQuaternary: '#5a6e8a',
        borderRadius: 10,
        borderRadiusLG: 14,
        borderRadiusSM: 7,
        borderRadiusXS: 5,
        controlHeight: 36,
        controlHeightSM: 30,
        fontSize: 14,
        fontFamily: 'var(--font-sans)',
        fontFamilyCode: 'var(--font-mono)',
        boxShadow: '0 6px 20px rgba(0, 0, 0, 0.36)',
        boxShadowSecondary: '0 12px 32px rgba(0, 0, 0, 0.44)',
      },
      components: {
        Card: {
          colorBgContainer: '#101726',
          colorBorderSecondary: '#202e46',
          borderRadiusLG: 14,
          headerFontSize: 14,
          headerHeight: 48,
        },
        Table: {
          colorBgContainer: 'transparent',
          headerBg: '#141e30',
          headerColor: '#b6c4d8',
          rowHoverBg: '#172338',
          borderColor: '#1e2c44',
          cellPaddingBlock: 11,
          cellPaddingInline: 14,
        },
        Tabs: {
          inkBarColor: '#60a5fa',
          itemSelectedColor: '#60a5fa',
          itemHoverColor: '#93c5fd',
          itemColor: '#94a3b8',
          horizontalItemPadding: '10px 4px',
        },
        Segmented: {
          trackBg: '#141e30',
          itemSelectedBg: '#1f2e47',
          itemSelectedColor: '#f1f5f9',
          itemColor: '#94a3b8',
          itemHoverColor: '#e2e8f0',
          borderRadius: 9,
        },
        Button: {
          borderRadius: 9,
          borderRadiusSM: 7,
          defaultBg: '#141e30',
          defaultBorderColor: '#253652',
          defaultColor: '#e2e8f0',
          defaultHoverBg: '#1b2840',
          defaultHoverBorderColor: '#3b82f6',
          defaultHoverColor: '#f8fafc',
          primaryShadow: '0 1px 2px rgba(0, 0, 0, 0.3)',
        },
        Input: {
          colorBgContainer: '#0d1422',
          colorBorder: '#24344f',
          hoverBorderColor: '#3b82f6',
          activeBorderColor: '#60a5fa',
          activeShadow: '0 0 0 2px rgba(59, 130, 246, 0.22)',
        },
        InputNumber: {
          colorBgContainer: '#0d1422',
          colorBorder: '#24344f',
          hoverBorderColor: '#3b82f6',
          activeBorderColor: '#60a5fa',
        },
        Select: {
          colorBgContainer: '#0d1422',
          colorBgElevated: '#162033',
          colorBorder: '#24344f',
          optionSelectedBg: 'rgba(59, 130, 246, 0.18)',
          optionActiveBg: '#1c2940',
        },
        Modal: {
          contentBg: '#101726',
          headerBg: '#101726',
          footerBg: '#101726',
        },
        Drawer: {
          colorBgElevated: '#101726',
        },
        Popover: {
          colorBgElevated: '#162033',
        },
        Tooltip: {
          colorBgSpotlight: '#1e2c45',
        },
        Tag: {
          borderRadiusSM: 6,
          defaultBg: '#152033',
          defaultColor: '#cbd5e1',
        },
        Alert: {
          borderRadiusLG: 10,
        },
        Switch: {
          handleWidth: 14,
          trackPadding: 2,
        },
      },
    };
  }

  return {
    token: {
      colorPrimary: '#2563eb',
      colorInfo: '#0284c7',
      colorSuccess: '#16a34a',
      colorWarning: '#d97706',
      colorError: '#dc2626',
      colorBgBase: '#ffffff',
      colorBgLayout: '#f4f6f9',
      colorBgContainer: '#ffffff',
      colorBgElevated: '#ffffff',
      colorFillAlter: '#f8fafc',
      colorFillSecondary: '#f1f5f9',
      colorFillTertiary: '#f8fafc',
      colorBorder: '#d8e0ea',
      colorBorderSecondary: '#e2e8f0',
      colorText: '#0f172a',
      colorTextSecondary: '#475569',
      colorTextTertiary: '#64748b',
      colorTextQuaternary: '#94a3b8',
      borderRadius: 10,
      borderRadiusLG: 14,
      borderRadiusSM: 7,
      borderRadiusXS: 5,
      controlHeight: 36,
      controlHeightSM: 30,
      fontSize: 14,
      fontFamily: 'var(--font-sans)',
      fontFamilyCode: 'var(--font-mono)',
      boxShadow: '0 4px 16px rgba(15, 23, 42, 0.06)',
      boxShadowSecondary: '0 10px 28px rgba(15, 23, 42, 0.09)',
    },
    components: {
      Card: {
        colorBgContainer: '#ffffff',
        colorBorderSecondary: '#e2e8f0',
        borderRadiusLG: 14,
        headerFontSize: 14,
        headerHeight: 48,
      },
      Table: {
        colorBgContainer: 'transparent',
        headerBg: '#f8fafc',
        headerColor: '#475569',
        rowHoverBg: '#f4f7fb',
        borderColor: '#e9eef5',
        cellPaddingBlock: 11,
        cellPaddingInline: 14,
      },
      Tabs: {
        inkBarColor: '#2563eb',
        itemSelectedColor: '#1d4ed8',
        itemHoverColor: '#2563eb',
        itemColor: '#64748b',
        horizontalItemPadding: '10px 4px',
      },
      Segmented: {
        trackBg: '#eef2f6',
        itemSelectedBg: '#ffffff',
        itemSelectedColor: '#0f172a',
        itemColor: '#475569',
        itemHoverColor: '#0f172a',
        borderRadius: 9,
      },
      Button: {
        borderRadius: 9,
        borderRadiusSM: 7,
        defaultBg: '#ffffff',
        defaultBorderColor: '#d8e0ea',
        defaultColor: '#1e293b',
        defaultHoverBg: '#f8fafc',
        defaultHoverBorderColor: '#93c5fd',
        defaultHoverColor: '#1d4ed8',
        primaryShadow: '0 1px 2px rgba(37, 99, 235, 0.22)',
      },
      Input: {
        colorBgContainer: '#ffffff',
        colorBorder: '#d8e0ea',
        hoverBorderColor: '#60a5fa',
        activeBorderColor: '#2563eb',
        activeShadow: '0 0 0 2px rgba(37, 99, 235, 0.14)',
      },
      InputNumber: {
        colorBgContainer: '#ffffff',
        colorBorder: '#d8e0ea',
        hoverBorderColor: '#60a5fa',
        activeBorderColor: '#2563eb',
      },
      Select: {
        colorBgContainer: '#ffffff',
        colorBorder: '#d8e0ea',
        optionSelectedBg: 'rgba(37, 99, 235, 0.08)',
        optionActiveBg: '#f1f5f9',
      },
      Tag: {
        borderRadiusSM: 6,
        defaultBg: '#f1f5f9',
        defaultColor: '#334155',
      },
      Alert: {
        borderRadiusLG: 10,
      },
      Switch: {
        handleWidth: 14,
        trackPadding: 2,
      },
    },
  };
}

export default function AccessibleConfigProvider({ theme, ...props }) {
  // Ant Design inserts a MotionProvider when this token first changes to false.
  // Read once to avoid remounting the app and losing unsaved edits on OS changes.
  const [reducedMotion] = useState(
    () => typeof window !== 'undefined' && window.matchMedia(REDUCED_MOTION_QUERY).matches,
  );

  const isDark = detectDarkAlgorithm(theme);

  const mergedTheme = useMemo(() => {
    const preset = buildDesignTokens(isDark);
    const incomingComponents = theme?.components || {};
    const mergedComponents = { ...preset.components };
    for (const [key, value] of Object.entries(incomingComponents)) {
      mergedComponents[key] = { ...(preset.components[key] || {}), ...value };
    }

    return {
      ...theme,
      token: {
        ...theme?.token,
        ...preset.token,
        motion: reducedMotion ? false : (theme?.token?.motion ?? true),
      },
      components: mergedComponents,
    };
  }, [isDark, reducedMotion, theme]);

  return <ConfigProvider {...props} theme={mergedTheme} />;
}
