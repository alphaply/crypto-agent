import { Grid, Select, Tabs } from 'antd';

// Keep every section reachable on small screens without a horizontal tab hunt.
export default function ResponsiveTabs({ items, activeKey, onChange, label, ...props }) {
  const screens = Grid.useBreakpoint();
  const compact = !screens.md;

  return (
    <div className="responsive-tabs">
      {compact ? (
        <label className="responsive-tabs__selector">
          <span>{label}</span>
          <Select
            aria-label={label}
            value={activeKey}
            onChange={onChange}
            options={items.map(({ key, label: title, disabled }) => ({ value: key, label: title, disabled }))}
          />
        </label>
      ) : null}
      <Tabs
        {...props}
        activeKey={activeKey}
        onChange={onChange}
        items={items}
        renderTabBar={compact ? () => null : undefined}
      />
    </div>
  );
}
