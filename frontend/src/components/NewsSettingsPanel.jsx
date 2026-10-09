import React, { useEffect, useState } from 'react';
import { Button, Card, Input, InputNumber, Select, Space, Switch, Tag, Typography } from 'antd';
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { usePreferences } from '../app/usePreferences';
import { PolymarketSettings } from './PolymarketPanel';
import NewsPipelineMonitor from './NewsPipelineMonitor';
export default function NewsSettingsPanel({ value = {}, onChange, providers = [], profiles = [], polymarket, onPolymarketChange, blockbeatsKey = {}, onBlockbeatsKeyChange }) {
  const { locale } = usePreferences(); const zh = locale === 'zh';
  const [status,setStatus]=useState(null); const [history,setHistory]=useState([]); const [error,setError]=useState(''); const [refreshing,setRefreshing]=useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let timer;
    const load = async () => {
      const results = await Promise.allSettled([
        api.get('/news/status', { silent: true, signal: controller.signal }),
        api.get('/news/runs', { params: { limit: 20 }, silent: true, signal: controller.signal }),
      ]);
      if (controller.signal.aborted) return;
      const [current, runs] = results;
      if (current.status === 'fulfilled') setStatus(current.value.data);
      if (runs.status === 'fulfilled') setHistory(runs.value.data.runs || []);
      setError(results.find((result) => result.status === 'rejected')?.reason?.message || '');
      timer = setTimeout(load, current.status === 'fulfilled' && current.value.data.running ? 2000 : 10000);
    };
    load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, []);
  const update=(patch)=>onChange({...value,...patch});
  const sources=value.sources||[];
  const editSource=(index,patch)=>update({sources:sources.map((item,i)=>i===index?{...item,...patch}:item)});
  const refresh=async()=>{setRefreshing(true);setError('');try{const {data}=await api.post('/news/refresh');setStatus(data);}catch(err){setError(err.message);}finally{setRefreshing(false);}};
  const modelOptions=(protocol)=>providers.filter((p)=>(p.api_protocol||'chat')===protocol).map((p)=>({value:p.provider_id,label:`${p.name || p.provider_id} · ${p.model}`}));
  return <div className="settings-stack">
    <NewsPipelineMonitor status={status} history={history} error={error} refreshing={refreshing} onRefresh={refresh} locale={locale} />
    <Card className="panel-card" title={zh?'消息聚合设置':'News aggregation settings'}>
      <div className="settings-form-grid">
        <label>{zh?'启用全局消息聚合':'Enable shared intelligence'}<Switch checked={value.enabled!==false} onChange={(enabled)=>update({enabled})}/></label>
        <div className="form-field settings-full-width">
          <label htmlFor="blockbeats-api-key">{zh?'律动 BlockBeats API Key':'BlockBeats API key'}</label>
          <Space.Compact style={{width:'100%'}}><Input.Password id="blockbeats-api-key" autoComplete="off" value={blockbeatsKey.value||''} placeholder={blockbeatsKey.configured?(zh?'已保存；留空保留原密钥':'Saved; leave blank to retain'):(zh?'输入律动 API Key':'Enter a BlockBeats API key')} onChange={(event)=>onBlockbeatsKeyChange?.({value:event.target.value,clear:false})}/><Button onClick={()=>onBlockbeatsKeyChange?.({value:'',clear:true})}>{zh?'清除':'Clear'}</Button></Space.Compact>
          <Typography.Text type="secondary">{zh?'用于获取律动快讯，密钥加密保存。':'Used to fetch BlockBeats newsflashes; stored encrypted. '}<a href="https://www.theblockbeats.info/apiDoc" target="_blank" rel="noreferrer">{zh?'申请 API Key / 接口文档':'Get an API key / documentation'}</a></Typography.Text>
        </div>
        <label>{zh?'摘要更新间隔（分钟）':'Refresh interval (minutes)'}<InputNumber min={5} max={1440} value={(value.refresh_seconds||3600)/60} onChange={(v)=>update({refresh_seconds:(v||60)*60})}/></label>
        <label>{zh?'Jev 评分模型':'Jev scoring model'}<Select allowClear value={value.scorer_provider_id||undefined} options={modelOptions('decisions')} placeholder={zh?'先添加 TypeSafe 官方 / B.AI Jev 渠道':'Add a TypeSafe / B.AI Jev provider first'} onChange={(v)=>update({scorer_provider_id:v||''})}/></label>
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
      {sources.map((source,index)=><div className="settings-source-row" key={source.id}><Switch size="small" checked={source.enabled!==false} onChange={(enabled)=>editSource(index,{enabled})}/><Input value={source.name} onChange={(e)=>editSource(index,{name:e.target.value})}/>{!['binance','blockbeats'].includes(source.kind) && source.url != null?<Input className="source-url" value={source.url} onChange={(e)=>editSource(index,{url:e.target.value})}/>:<Tag className="source-url">{source.kind==='blockbeats'?'BlockBeats API':source.kind}</Tag>}<Button type="text" danger icon={<DeleteOutlined />} aria-label={zh?'删除来源':'Remove source'} onClick={()=>update({sources:sources.filter((_,i)=>i!==index)})}/></div>)}
    </Card>
    <PolymarketSettings value={polymarket} onChange={onPolymarketChange}/>
  </div>;
}
