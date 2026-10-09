"""Local, synthetic-only UI fixture. Run: uv run python tests/ui_preview.py.
Never loads runtime configuration or connects to exchanges/model providers.
"""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from backend.app.schemas.news import default_news_settings, default_pricing_sync_settings
import uvicorn

app = FastAPI()
now = datetime.now(timezone.utc).isoformat()
report = dict(version=1, market_analysis='市场量能趋稳，价格仍在区间内运行。观察收盘后的突破确认。', strategy='等待关键位置确认后再评估入场。', decision={'action':'HOLD','rationale':'趋势证据尚未形成共振，保留组合额度。'}, forecast={'next_1h':'若突破区间上沿且量能增加，则重新评估多头机会。','next_4h':'维持结构观察；跌破支撑时取消偏多判断。'}, risks=['波动扩大与消息冲击'], invalidation='收盘跌破支撑区域。', next_watchpoints=['下一根小时 K 线收盘','资金费率与成交量'], summary='观望，等待确认。', execution_results=[], validation_status='valid')
agents = [dict(config_id=cid,title=title,symbol=sym,symbols=[sym],enabled=True,mode=mode,model='demo-model',timestamp=now,content='观望，等待确认。',strategy_logic='等待关键位置确认。',report=report,run_interval=60,leverage=3,market_profile='hourly',market_timeframes=['1h','4h','1d'],frequency='60m',next_run='10-08 21:00',schedule={'state':'scheduled','frequency':'60m','next_run':'10-08 21:00','timezone':'Asia/Shanghai'},llm_provider_id='demo',summarizer_provider_id='demo',exchange_profile_id='demo-account',secrets={},summarizer={}) for cid,title,sym,mode in [('btc-trend','BTC 趋势观察','BTC/USDT','MOCK'),('eth-structure','ETH 结构策略','ETH/USDT','MOCK'),('spot-plan','现货组合计划','SOL/USDT','SPOT_DCA')]]
providers=[dict(provider_id='demo',name='研究模型',model='demo-model',api_base='https://example.invalid/v1',temperature=0.3,api_protocol='chat',pricing_mode='manual',input_price_per_m=1,output_price_per_m=2,secrets={'api_key':{'configured':True,'value':'demo-key-not-real'}},api_key_configured=True),dict(provider_id='jev',name='Jev 评分',model='jev-1.13.0',api_base='https://api.b.ai/v1',api_protocol='decisions',secrets={'api_key':{'configured':False,'value':''}})]
profiles=[dict(profile_id='demo-account',name='模拟账户',exchange='binance',market_type='spot',configured=True,supported_market_types=['spot','swap'],secrets={key:{'configured':True,'value':'demo-not-real'} for key in ['api_key','secret','passphrase']})]
news=dict(id=1,timestamp=now,raw=dict(as_of=now,digest='### 本轮市场观察\n\n市场维持区间震荡，交易所公告与宏观政策是本轮重点。**统一情报已同步到所有任务。**\n\n- 新增：交易所上线与维护公告，关注流动性变化。\n- 持续观察：风险偏好与资金流向，等待价格和成交量确认。',items=[dict(title=title,source=source,url='https://example.com',scoring={'score':score,'confidence':0.9}) for title,source,score in [('律动快讯：市场资金流向与最新事件','BlockBeats',80),('币安公告：交易服务维护通知','Binance',60),('宏观政策与风险资产联动观察','政策动态',75)]],source_health={'blockbeats':{'status':'ok','count':12},'binance':{'status':'pending_configuration','error':'待配置公告凭据'}},candidate_count=45,scored_count=40,filtered_count=3,status='degraded'))
usage={'daily':[],'models':[],'agents':[],'providers':[],'purposes':[],'today':{'cost':0.125,'unpriced_calls':1,'costs_by_currency':{'USD':0.125}},'summary':{'cost':0.125,'unpriced_calls':1,'costs_by_currency':{'USD':0.125}}}
config={'globals':{'enable_scheduler':True,'market_timeframes':['1h','4h','1d'],'news':default_news_settings(),'pricing_sync':default_pricing_sync_settings(),'secrets':{},'polymarket':{}},'agents':agents,'llm_providers':providers,'exchange_profiles':profiles,'options':{'market_timeframes':['15m','1h','4h','1d','1w'],'trading_modes':['MOCK','REAL','SPOT_DCA'],'exchanges':['binance','okx']},'prompts':{'files':[]},'pricing':[]}
sessions=[]
@app.api_route('/api/{path:path}',methods=['GET','POST','PUT','DELETE'])
async def fixtures(path:str,request:Request):
    if path=='setup/status': return {'required':False}
    if path=='auth/login': return {'token':'synthetic-ui-token','user':{'name':'UI test'}}
    if path=='auth/me': return {'user':{'name':'UI test'}}
    if path=='config': return config
    if path=='public/dashboard': return {'agent_summaries':agents,'compare_candidates':agents,'default_compare_ids':[a['config_id'] for a in agents],'overview_metrics':{'enabled_count':3,'agent_count':3,'runs_24h':{'finished':36,'failed':1,'running':0}},'usage_today':usage['today'],'news_snapshot':news,'symbols':[a['symbol'] for a in agents]}
    if path=='public/compare': return {'series':[],'rows':[]}
    if path.startswith('public/workspace/'):
        agent=next(a for a in agents if a['config_id']==path.split('/')[-1]); timeframe=request.query_params.get('timeframe','1h')
        return {'agent':agent,'market_timeframes':['1h','4h','1d'],'timeframe':timeframe,'position':{'positions':[],'mode':agent['mode'],'balance':1000,'margin_balance':1000},'orders':{'orders':[]},'short_memories':{'short_memories':[{'id':1,'config_id':agent['config_id'],'bucket_start':now,'bucket_end':now,'market_summary':'当前处于区间结构，保持单一主策略；下一轮验证突破量能。','source_count':4}]},'kline':{'symbol':agent['symbol'],'timeframe':timeframe,'candles':[],'positions':[],'pending_orders':[]}}
    if path=='public/usage': return usage
    if 'short-memories' in path: return {'short_memories':[]}
    if path=='news/status': return {**news['raw'],'last_success_at':now}
    if path=='news/refresh': return {'status':'running'}
    if path=='pricing/sync/status': return {'status':'idle','last_success_at':now}
    if path=='pricing/catalog': return {'models':[],'total':0}
    if path=='mcp': return {'settings':{'enabled':False,'public_url':'http://localhost:7860'},'profiles':[],'keys':[],'tools':['get_news','get_market','execute_trade'],'scopes':['read','trade','cancel'],'connection':{'url':'http://localhost:7860/mcp'},'audit':[]}
    if path=='chat/bootstrap': return {'configs':agents,'exchange_profiles':profiles,'llm_providers':providers[:1],'sessions':sessions,'temporary_chat':{'timeframes':['1h','4h','1d']}}
    if path=='chat/sessions' and request.method=='POST':
        data=await request.json();session={'session_id':'demo-chat','title':'新建任务','session_type':data.get('mode','temporary'),'runtime':data.get('runtime',{}),'config_id':data.get('config_id',''),'symbol':data.get('runtime',{}).get('symbol','BTC/USDT')};sessions.insert(0,session);return {'session_id':session['session_id'],'session':session}
    if path.startswith('chat/sessions/'): return {'session':sessions[0] if sessions else {},'messages':[],'pending_approval':None}
    if path.startswith('config/schedule-preview'):
        from backend.utils.run_schedule import preview_dca_schedule
        return {'next_runs':preview_dca_schedule((await request.json())['config'],count=3)}
    return {'success':True,'items':[],'rules':[],'runs':[],'data':[],'symbols':[{'symbol':'BTC/USDT','display_name':'BTC/USDT'}]}
root=Path(__file__).resolve().parents[1]/'frontend'/'dist'
app.mount('/assets',StaticFiles(directory=root/'assets'),name='assets')
@app.get('/{path:path}')
def page(path:str): return FileResponse(root/'index.html')
if __name__=='__main__': uvicorn.run(app,host='127.0.0.1',port=7861)
