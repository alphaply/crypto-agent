import React, { useEffect, useState } from 'react';
import { Alert, Button, Card, Input, InputNumber, Select, Space, Switch, Table, Tag, Typography } from 'antd';
import { DeleteOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { usePreferences } from '../app/usePreferences';
import { PolymarketSettings } from './PolymarketPanel';
export default function NewsSettingsPanel({ value = {}, onChange, providers = [], profiles = [], polymarket, onPolymarketChange }) {
  const { locale } = usePreferences(); const zh = locale === 'zh';
  const [status,setStatus]=useState(null); const [error,setError]=useState(''); const [refreshing,setRefreshing]=useState(false);
  const load = () => api.get('/news/status',{silent:true}).then((res)=>setStatus(res.data)).catch((err)=>setError(err.message));
  useEffect(()=>{load(); const timer=setInterval(load,15000); return ()=>clearInterval(timer);},[]);
  const update=(patch)=>onChange({...value,...patch});
  const sources=value.sources||[];
  const editSource=(index,patch)=>update({sources:sources.map((item,i)=>i===index?{...item,...patch}:item)});
  const refresh=async()=>{setRefreshing(true);setError('');try{await api.post('/news/refresh');await load();}catch(err){setError(err.message);}finally{setRefreshing(false);}};
  const modelOptions=(protocol)=>providers.filter((p)=>(p.api_protocol||'chat')===protocol).map((p)=>({value:p.provider_id,label:`${p.name || p.provider_id} · ${p.model}`}));
  const sourceHealth=status?.sources || status?.source_health || status?.status?.sources || [];
  const health=Array.isArray(sourceHealth)?sourceHealth:Object.entries(sourceHealth).map(([id,row])=>({id,...row}));
  return <div className="settings-stack">
    <Card className="panel-card" title={zh?'消息聚合':'News aggregation'} extra={<Button icon={<ReloadOutlined />} onClick={refresh} loading={refreshing}>{zh?'立即刷新':'Refresh now'}</Button>}>
      {error&&<Alert type="error" title={error} closable onClose={()=>setError('')} style={{marginBottom:16}}/>}
      <div className="settings-form-grid">
        <label>{zh?'启用全局消息聚合':'Enable shared intelligence'}<Switch checked={value.enabled!==false} onChange={(enabled)=>update({enabled})}/></label>
        <label>{zh?'摘要更新间隔（分钟）':'Refresh interval (minutes)'}<InputNumber min={5} max={1440} value={(value.refresh_seconds||3600)/60} onChange={(v)=>update({refresh_seconds:(v||60)*60})}/></label>
        <label>{zh?'Jev 评分模型':'Jev scoring model'}<Select allowClear value={value.scorer_provider_id||undefined} options={modelOptions('decisions')} placeholder={zh?'先在模型配置添加 Jev / Decisions 渠道':'Add a Jev / Decisions provider first'} onChange={(v)=>update({scorer_provider_id:v||''})}/></label>
        <label>{zh?'摘要模型':'Summary model'}<Select allowClear value={value.summarizer_provider_id||undefined} options={modelOptions('chat')} onChange={(v)=>update({summarizer_provider_id:v||''})}/></label>
        <label>{zh?'最低相关度（0–100）':'Minimum relevance (0–100)'}<InputNumber min={0} max={100} value={value.min_score??60} onChange={(v)=>update({min_score:v??60})}/></label>
        <label>{zh?'消息窗口（小时）':'Lookback (hours)'}<InputNumber min={1} max={168} value={value.lookback_hours||24} onChange={(v)=>update({lookback_hours:v||24})}/></label>
        <label>{zh?'候选条数上限':'Candidate limit'}<InputNumber min={value.max_items||20} max={300} value={value.candidate_limit||80} onChange={(v)=>update({candidate_limit:v||80})}/></label>
        <label>{zh?'摘要纳入条数':'Summary item limit'}<InputNumber min={1} max={Math.min(value.candidate_limit||80,100)} value={value.max_items||20} onChange={(v)=>update({max_items:v||20})}/></label>
        <label className="settings-full-width">{zh?'币安公告连接账户':'Binance announcement credentials'}<Select allowClear value={value.binance_exchange_profile_id||undefined} options={profiles.filter((p)=>p.exchange==='binance').map((p)=>({value:p.profile_id,label:p.name||p.profile_id}))} onChange={(v)=>update({binance_exchange_profile_id:v||''})} placeholder={zh?'选择已保存的 Binance 账户':'Choose a saved Binance profile'}/></label>
      </div>
      <details style={{marginTop:20}}><summary>{zh?'评分标准':'Scoring rubric'}</summary><div className="settings-stack" style={{marginTop:12}}><Input.TextArea rows={3} value={value.scorer_instructions||''} onChange={(e)=>update({scorer_instructions:e.target.value})} placeholder={zh?'相关度评分指令':'Relevance instructions'}/>{(value.scorer_criteria||[]).map((criterion,index)=><Input key={index} addonBefore={index} value={criterion} onChange={(e)=>update({scorer_criteria:value.scorer_criteria.map((item,i)=>i===index?e.target.value:item)})}/>)}</div></details>
    </Card>
    <Card className="panel-card" title={zh?'消息来源':'Sources'} extra={<Button icon={<PlusOutlined />} onClick={()=>update({sources:[...sources,{id:'rss_'+Date.now(),name:'RSS',kind:'rss',url:'https://example.com/feed',enabled:false,category:'crypto'}]})}>RSS</Button>}>
      {sources.map((source,index)=><div className="settings-source-row" key={source.id}><Switch size="small" checked={source.enabled!==false} onChange={(enabled)=>editSource(index,{enabled})}/><Input value={source.name} onChange={(e)=>editSource(index,{name:e.target.value})}/>{source.kind !== 'binance' && source.url != null?<Input className="source-url" value={source.url} onChange={(e)=>editSource(index,{url:e.target.value})}/>:<Tag className="source-url">{source.kind}</Tag>}<Button type="text" danger icon={<DeleteOutlined />} aria-label={zh?'删除来源':'Remove source'} onClick={()=>update({sources:sources.filter((_,i)=>i!==index)})}/></div>)}
    </Card>
    <Card className="panel-card" title={zh?'处理状态':'Pipeline status'}><Space direction="vertical" style={{width:'100%'}}>{status?.error&&<Alert type="warning" showIcon title={status.error}/>}<Space wrap><Tag>{status?.status||"idle"}</Tag><Tag>{zh?"采集":"Collected"}: {status?.candidate_count??0}</Tag><Tag>{zh?"评分":"Scored"}: {status?.scored_count??0}</Tag><Tag>{zh?"保留":"Selected"}: {status?.filtered_count??0}</Tag></Space><Typography.Text type="secondary">{status?.last_success_at||status?.last_success||status?.status?.last_success_at||'—'}</Typography.Text>{Array.isArray(health)&&health.length?<Table size="small" rowKey={(row)=>row.id||row.source_id||row.name} dataSource={health} pagination={false} scroll={{x:520}} columns={[{title:zh?'来源':'Source',render:(_,row)=>row.name||row.source_id||row.id},{title:zh?'状态':'Status',dataIndex:'status'},{title:zh?'详情':'Detail',render:(_,row)=>row.error||row.message||row.count||'—'}]}/>:<Typography.Text type="secondary">{zh?'配置完成后显示采集与摘要处理结果。':'Collection and summary status will appear after configuration.'}</Typography.Text>}</Space></Card>
    <PolymarketSettings value={polymarket} onChange={onPolymarketChange}/>
  </div>;
}
