import React, { useEffect, useState } from 'react';
import { Button, Card, Input, InputNumber, Select, Space, Switch, Tag, Typography } from 'antd';
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { usePreferences } from '../app/usePreferences';
import { PolymarketSettings } from './PolymarketPanel';
import NewsPipelineMonitor from './NewsPipelineMonitor';
import NewsTriggerSettings from './NewsTriggerSettings';
export default function NewsSettingsPanel({ value = {}, onChange, providers = [], profiles = [], agents = [], polymarket, onPolymarketChange, blockbeatsKey = {}, onBlockbeatsKeyChange }) {
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
        <label>{zh?'消息抓取间隔（分钟）':'Fetch interval (minutes)'}<InputNumber min={1} max={1440} value={(value.refresh_seconds||3600)/60} onChange={(v)=>update({refresh_seconds:(v||60)*60})}/></label>
        <label>{zh?'评分方式':'Scoring mode'}<Select value={value.scoring_mode||'jev'} options={[{value:'off',label:zh?'关闭评分，按时间选取':'Off; select by time'},{value:'jev',label:'Jev'},{value:'llm',label:zh?'普通 LLM 批量评分':'Generative LLM batch scoring'}]} onChange={(scoring_mode)=>update({scoring_mode,scorer_provider_id:''})}/></label>
        {(value.scoring_mode||'jev')!=='off' && <label>{zh?'评分模型':'Scoring model'}<Select showSearch optionFilterProp="label" allowClear value={value.scorer_provider_id||undefined} options={modelOptions(value.scoring_mode==='llm'?'chat':'decisions')} onChange={(v)=>update({scorer_provider_id:v||''})}/></label>}
        <label>{zh?'摘要模型':'Summary model'}<Select allowClear value={value.summarizer_provider_id||undefined} options={modelOptions('chat')} onChange={(v)=>update({summarizer_provider_id:v||''})}/></label>
        <label>{zh?'最低相关度（0–100）':'Minimum relevance (0–100)'}<InputNumber disabled={value.scoring_mode==='off'} min={0} max={100} value={value.min_score??60} onChange={(v)=>update({min_score:v??60})}/></label>
        <label>{zh?'摘要模式':'Summary mode'}<Select value={value.summary_mode||'rolling'} options={[{value:'rolling',label:zh?'旧摘要 + 当前原文 → 更新摘要':'Previous digest + current sources'},{value:'full',label:zh?'只根据当前原文重建':'Rebuild from current sources'}]} onChange={(summary_mode)=>update({summary_mode})}/></label>
        <label>{zh?'摘要最小生成间隔（分钟）':'Minimum summary interval (minutes)'}<InputNumber min={0} max={1440} value={(value.summary_min_seconds??300)/60} onChange={(v)=>update({summary_min_seconds:(v??5)*60})}/></label>
        <label>{zh?'币安推送加速刷新（最短 1 分钟）':'Accelerate refresh on Binance push (min 1 minute)'}<Switch checked={value.stream_refresh_enabled===true} onChange={(stream_refresh_enabled)=>update({stream_refresh_enabled})}/></label>
        <label>{zh?'消息 LangSmith 追踪':'News LangSmith tracing'}<Select value={value.trace_mode||'off'} options={[{value:'off',label:zh?'关闭，仅保留本地记录':'Off; local records only'},{value:'summary',label:zh?'每轮只记录一条概要':'One root per refresh'},{value:'full',label:zh?'完整追踪（排障）':'Full (debugging)'}]} onChange={(trace_mode)=>update({trace_mode})}/></label>
        <label className="settings-full-width">{zh?'摘要补充要求':'Additional summary instructions'}<Input.TextArea rows={3} maxLength={5000} value={value.summary_instructions||''} onChange={(e)=>update({summary_instructions:e.target.value})} placeholder={zh?'例如：优先整理 CPI 公布值、预期值、前值；缺失时明确说明，按宏观/行业分类。':'Example: organize CPI actual, forecast and prior values; mark missing values.'}/></label>
        <label>{zh?'消息窗口（小时）':'Lookback (hours)'}<InputNumber min={1} max={168} value={value.lookback_hours||24} onChange={(v)=>update({lookback_hours:v||24})}/></label>
        <label>{zh?'候选条数上限':'Candidate limit'}<InputNumber min={value.max_items||20} max={300} value={value.candidate_limit||80} onChange={(v)=>update({candidate_limit:v||80})}/></label>
        <label>{zh?'摘要纳入条数':'Summary item limit'}<InputNumber min={1} max={Math.min(value.candidate_limit||80,100)} value={value.max_items||20} onChange={(v)=>update({max_items:v||20})}/></label>
        <label className="settings-full-width">{zh?'币安公告连接账户':'Binance announcement credentials'}<Select allowClear value={value.binance_exchange_profile_id||undefined} options={profiles.filter((p)=>p.exchange==='binance').map((p)=>({value:p.profile_id,label:p.name||p.profile_id}))} onChange={(v)=>update({binance_exchange_profile_id:v||''})} placeholder={zh?'选择已保存的 Binance 账户':'Choose a saved Binance profile'}/></label>
      </div>
      <Typography.Paragraph type="secondary" style={{marginTop:16}}>{zh?'输入：API / RSS / 日历 / 币安推送 → 时间过滤、去重 → 可选评分 → 筛选 → 摘要 → 共享快照。普通 LLM 每批最多 20 条，已评分的相同内容复用缓存；摘要输入不变不调用模型。摘要等待间隔时保留旧摘要及其引用，新消息仍独立参与触发规则。日历是预告，不代表已公布数据。':'API / RSS / calendars / Binance stream → time filter and deduplication → optional scoring → selection → digest → shared snapshot. LLM scoring uses batches of up to 20 and cached results. Unchanged summary inputs skip model calls. Deferred summaries retain their original references; new articles independently feed trigger rules. Calendars describe scheduled events, not released figures.'}</Typography.Paragraph>
      <details style={{marginTop:20}}><summary>{zh?'评分标准':'Scoring rubric'}</summary><div className="settings-stack" style={{marginTop:12}}><Input.TextArea rows={3} value={value.scorer_instructions||''} onChange={(e)=>update({scorer_instructions:e.target.value})} placeholder={zh?'相关度评分指令':'Relevance instructions'}/>{(value.scorer_criteria||[]).map((criterion,index)=><Input key={index} addonBefore={index} value={criterion} onChange={(e)=>update({scorer_criteria:value.scorer_criteria.map((item,i)=>i===index?e.target.value:item)})}/>)}</div></details>
    </Card>
    <NewsTriggerSettings rules={value.trigger_rules} onChange={(trigger_rules)=>update({trigger_rules})} agents={agents} providers={providers} sources={sources} status={status?.triggers} zh={zh}/>
    <Card className="panel-card" title={zh?'消息来源':'Sources'} extra={<Button icon={<PlusOutlined />} onClick={()=>update({sources:[...sources,{id:'rss_'+Date.now(),name:'RSS',kind:'rss',url:'https://example.com/feed',enabled:false,category:'crypto'}]})}>RSS</Button>}>
      {sources.map((source,index)=><div className="settings-source-row" key={source.id}><Switch size="small" checked={source.enabled!==false} onChange={(enabled)=>editSource(index,{enabled})}/><Input value={source.name} onChange={(e)=>editSource(index,{name:e.target.value})}/>{!['binance','blockbeats'].includes(source.kind) && source.url != null?<Input className="source-url" value={source.url} onChange={(e)=>editSource(index,{url:e.target.value})}/>:<Tag className="source-url">{source.kind==='blockbeats'?'BlockBeats API':source.kind}</Tag>}<Button type="text" danger icon={<DeleteOutlined />} aria-label={zh?'删除来源':'Remove source'} onClick={()=>update({sources:sources.filter((_,i)=>i!==index)})}/></div>)}
    </Card>
    <PolymarketSettings value={polymarket} onChange={onPolymarketChange}/>
  </div>;
}
