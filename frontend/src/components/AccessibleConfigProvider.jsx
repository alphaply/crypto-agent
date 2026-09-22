import { useState } from 'react';
import { ConfigProvider } from 'antd';

const query = '(prefers-reduced-motion: reduce)';
export default function AccessibleConfigProvider({ theme, ...props }) {
  // Ant Design inserts a MotionProvider when this token first changes to false.
  // Read once to avoid remounting the app and losing unsaved edits on OS changes.
  const [reducedMotion] = useState(() => typeof window !== 'undefined' && window.matchMedia(query).matches);
  return (
    <ConfigProvider
      {...props}
      theme={{ ...theme, token: { ...theme?.token, motion: reducedMotion ? false : (theme?.token?.motion ?? true) } }}
    />
  );
}
