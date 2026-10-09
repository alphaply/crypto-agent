import React, { useEffect, useState } from 'react';
import { Alert, Button, Input, Select, Space, Tag, Typography } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { usePreferences } from '../app/usePreferences';
export default function SpotScheduleEditor({ config, onChange }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const schedule = config.dca_schedule || { frequency: ['1w', 'weekly'].includes(config.dca_freq) ? 'weekly' : 'daily', days: [config.dca_weekday ?? 0], times: [config.dca_time || '09:00'], timezone: 'Asia/Shanghai' };
  const [clock, setClock] = useState('21:00');
  const [preview, setPreview] = useState([]);
  const [error, setError] = useState('');
  const signature = JSON.stringify(schedule);
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      api.post('/config/schedule-preview', { config: { mode: 'SPOT_DCA', dca_schedule: JSON.parse(signature) } }, { signal: controller.signal, silent: true }).then((res) => { setPreview(res.data.next_runs || []); setError(''); }).catch((err) => { if (!controller.signal.aborted) setError(err.message); });
    }, 350);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [signature]);
  const update = (patch) => onChange({ ...schedule, ...patch });
  const days = zh ? ['周一','周二','周三','周四','周五','周六','周日'] : ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  return <div className="settings-stack settings-full-width">
    <div className="settings-form-grid">
      <label>{zh ? '执行频率' : 'Frequency'}<Select value={schedule.frequency} options={[{ value:'daily',label:zh?'每天':'Daily' },{ value:'weekly',label:zh?'每周':'Weekly' }]} onChange={(frequency) => update({frequency})} /></label>
      <label>{zh ? '时区' : 'Time zone'}<Select showSearch value={schedule.timezone} options={['Asia/Shanghai','UTC','Asia/Hong_Kong','Asia/Tokyo','Europe/London','America/New_York'].map((value)=>({value,label:value}))} onChange={(timezone)=>update({timezone})} /></label>
      {schedule.frequency === 'weekly' && <label className="settings-full-width">{zh ? '执行星期' : 'Weekdays'}<Select mode="multiple" value={schedule.days} options={days.map((label,value)=>({label,value}))} onChange={(value)=>{if(value.length)update({days:value});}} /></label>}
      <label className="settings-full-width">{zh ? '执行时点' : 'Run times'}<Space wrap>{schedule.times.map((value)=><Tag key={value} closable={schedule.times.length>1} onClose={()=>update({times:schedule.times.filter((item)=>item!==value)})}>{value}</Tag>)}</Space><Space><Input type="time" value={clock} onChange={(e)=>setClock(e.target.value)} /><Button icon={<PlusOutlined />} disabled={!clock || schedule.times.includes(clock)} onClick={()=>update({times:[...schedule.times,clock].sort()})}>{zh?'添加':'Add'}</Button></Space></label>
    </div>
    {error && <Alert type="error" title={error} />}
    <Typography.Text type="secondary">{zh?'接下来三次：':'Next three runs: '}{preview.map((value)=>new Intl.DateTimeFormat(zh?'zh-CN':'en-GB',{timeZone:schedule.timezone,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value))).join(' · ') || '—'} ({schedule.timezone})</Typography.Text>
    <Typography.Text type="secondary">{zh?'每次运行获得一份全标的共用额度；停机只补当前周期内最近一次。':'Each run receives one shared portfolio allowance. Only the latest missed slot in the current period is recovered.'}</Typography.Text>
  </div>;
}
