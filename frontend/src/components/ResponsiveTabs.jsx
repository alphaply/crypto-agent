import { Grid, Select, Tabs } from 'antd';

// Keep every section reachable on small screens without a horizontal tab hunt.
export default function ResponsiveTabs({
  items = [],
  activeKey,
  onChange,
  label,
  variant = 'line',
  className = '',
  ...props
}) {
  const screens = Grid.useBreakpoint();
  const compact = !screens.md;

  const rootClassName = [
    'responsive-tabs',
    variant === 'pill' ? 'responsive-tabs--pill' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <div className={rootClassName}>
      {compact ? (
        <label className="responsive-tabs__selector">
          {label ? <span className="responsive-tabs__label">{label}</span> : null}
          <Select
            aria-label={label}
            value={activeKey}
            onChange={onChange}
            size="middle"
            options={items.map(({ key, label: title, disabled }) => ({
              value: key,
              label: title,
              disabled,
            }))}
          />
        </label>
      ) : null}
      <Tabs
        {...props}
        activeKey={activeKey}
        onChange={onChange}
        items={items}
        renderTabBar={compact ? () => null : props.renderTabBar}
      />
    </div>
  );
}
