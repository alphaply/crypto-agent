import React from 'react';
import { Alert, Button, Card, Input, InputNumber, Select, Space, Typography } from 'antd';
import { ArrowDownOutlined, ArrowUpOutlined, DeleteOutlined, PlusOutlined } from '@ant-design/icons';
import { usePreferences } from '../app/usePreferences';

const weekdays = [0, 1, 2, 3, 4];
const zones = ['Asia/Shanghai', 'America/New_York', 'UTC', 'Europe/London', 'Asia/Tokyo'];

export default function RunScheduleEditor({ value = [], onChange, onPreset }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const text = (cn, en) => zh ? cn : en;
  const update = (index, field, next) => onChange(value.map((rule, i) => i === index ? { ...rule, [field]: next } : rule));
  const move = (index, offset) => {
    const next = [...value];
    [next[index], next[index + offset]] = [next[index + offset], next[index]];
    onChange(next);
  };
  const preset = () => onPreset([
    { name: text('周末', 'Weekend'), days: [5, 6], start: '00:00', end: '24:00', interval: 30, timezone: 'Asia/Shanghai' },
    { name: text('美盘（纽约时段）', 'US session (New York)'), days: weekdays, start: '09:30', end: '16:00', interval: 20, timezone: 'America/New_York' },
    { name: text('亚盘', 'Asian session'), days: weekdays, start: '08:00', end: '16:00', interval: 30, timezone: 'Asia/Shanghai' },
  ]);

  return <div style={{ gridColumn: '1 / -1', minWidth: 0 }}>
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Typography.Text strong>{text('按时段调整运行频率', 'Time-based run intervals')}</Typography.Text>
      <Alert type="info" showIcon title={text('从上到下匹配，第一条生效；其余时间使用默认间隔。', 'Rules match from top to bottom; the first match wins. Other times use the default interval.')}
        description={text('每个时段从开始时间起按间隔运行，结束时间不含在内。跨午夜时，星期指开始那一天；全天填 00:00—24:00。纽约时区自动适配夏令时。止盈止损监控独立运行。', 'Intervals start at the window opening; the end is exclusive. Overnight weekdays refer to the start day; use 00:00–24:00 for a full day. New York observes DST automatically. Position protection runs independently.')} />
      <Button style={{ whiteSpace: 'normal', height: 'auto', minHeight: 32, maxWidth: '100%' }} onClick={preset}>{text('应用预设：亚盘 30 / 美盘 20 / 周末 30 分钟', 'Apply preset: Asia 30 / US 20 / weekend 30 min')}</Button>
      <Typography.Text type="secondary">{text('预设会替换下方规则，并把默认间隔设为 30 分钟；保存任务后，还需点击页面上的“保存配置”生效。美盘默认参考纽约工作日 09:30–16:00，可自行扩展。', 'The preset replaces these rules and sets the default to 30 minutes. Save the task, then click Save Config to apply. US hours default to weekdays 09:30–16:00 New York and can be extended.')}</Typography.Text>
      {value.map((rule, index) => <Card key={index} size="small" title={`${index + 1}. ${rule.name || text('时段', 'Window')}`} extra={<Space size={4}>
        <Button aria-label={text('提高优先级', 'Higher priority')} size="small" icon={<ArrowUpOutlined />} disabled={index === 0} onClick={() => move(index, -1)} />
        <Button aria-label={text('降低优先级', 'Lower priority')} size="small" icon={<ArrowDownOutlined />} disabled={index === value.length - 1} onClick={() => move(index, 1)} />
        <Button aria-label={text('删除时段', 'Remove window')} size="small" danger icon={<DeleteOutlined />} onClick={() => onChange(value.filter((_, i) => i !== index))} />
      </Space>}>
        <div className="field-grid">
          <div className="form-field"><label>{text('名称', 'Name')}</label><Input maxLength={80} value={rule.name} onChange={e => update(index, 'name', e.target.value)} /></div>
          <div className="form-field"><label>{text('时区', 'Timezone')}</label><Select showSearch value={rule.timezone} options={[...new Set([...zones, rule.timezone])].filter(Boolean).map(zone => ({ value: zone, label: zone }))} onChange={v => update(index, 'timezone', v)} /></div>
          <div className="form-field" style={{ gridColumn: '1 / -1' }}><label>{text('星期（按所选时区）', 'Weekdays (selected timezone)')}</label><Select mode="multiple" value={rule.days} status={!rule.days?.length ? 'error' : undefined} options={(zh ? ['周一', '周二', '周三', '周四', '周五', '周六', '周日'] : ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']).map((label, day) => ({ label, value: day }))} onChange={v => update(index, 'days', v)} /></div>
          <div className="form-field"><label>{text('开始 HH:mm', 'Start HH:mm')}</label><Input value={rule.start} placeholder="08:00" status={!/^([01]\d|2[0-3]):[0-5]\d$/.test(rule.start) ? 'error' : undefined} onChange={e => update(index, 'start', e.target.value)} /></div>
          <div className="form-field"><label>{text('结束 HH:mm（支持 24:00）', 'End HH:mm (24:00 allowed)')}</label><Input value={rule.end} placeholder="16:00" status={(!/^(([01]\d|2[0-3]):[0-5]\d|24:00)$/.test(rule.end) || rule.end === rule.start) ? 'error' : undefined} onChange={e => update(index, 'end', e.target.value)} /></div>
          <div className="form-field"><label>{text('每隔多少分钟运行', 'Run every (minutes)')}</label><InputNumber min={15} max={1440} precision={0} value={rule.interval} onChange={v => update(index, 'interval', v ?? 30)} style={{ width: '100%' }} /></div>
        </div>
      </Card>)}
      <Button icon={<PlusOutlined />} disabled={value.length >= 20} onClick={() => onChange([...value, { name: text('新时段', 'New window'), days: [...weekdays], start: '08:00', end: '16:00', interval: 30, timezone: 'Asia/Shanghai' }])}>{text('添加时段', 'Add window')}</Button>
    </Space>
  </div>;
}
