"""Local, synthetic-only UI fixture. Run: uv run python tests/ui_preview.py.
Never loads runtime configuration or connects to exchanges/model providers.
"""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import sys
import math
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from backend.app.schemas.news import default_news_settings, default_pricing_sync_settings
import uvicorn

app = FastAPI()
now = datetime.now(timezone.utc).isoformat()
decision = dict(version=1, summary_status='completed', execution_results=[], messages=[
    dict(role='assistant', content='先查询账户与当前委托，确认本轮可用额度。', reasoning_content='核验账户事实后再判断，避免重复下单。', tool_calls=[dict(id='inspect-1',name='get_account',args={'symbol':'BTC/USDT'})]),
    dict(role='tool', tool='get_account', tool_call_id='inspect-1', status='completed', result='{"status":"completed","positions":[],"open_orders":[]}'),
    dict(role='assistant', content='当前无持仓和委托。量价尚未确认，继续等待，不新开仓。', tool_calls=[]),
])
agents = [dict(config_id=cid,title=title,symbol=sym,symbols=[sym],enabled=True,mode=mode,model='demo-model',timestamp=now,content='观望，等待确认。',strategy_logic='等待关键位置确认。',decision=decision,memory_update={"status":"completed","attempts":0,"error":""},run_interval=60,leverage=3,market_profile='hourly',market_timeframes=['1h','4h','1d'],frequency='60m',next_run='10-08 21:00',schedule={'state':'scheduled','frequency':'60m','next_run':'10-08 21:00','timezone':'Asia/Shanghai'},llm_provider_id='demo',summarizer_provider_id='demo',exchange_profile_id='demo-account',secrets={},summarizer={}) for cid,title,sym,mode in [('btc-trend','BTC 趋势观察','BTC/USDT','MOCK'),('eth-structure','ETH 结构策略','ETH/USDT','MOCK'),('spot-plan','现货组合计划','SOL/USDT','SPOT_DCA')]]
providers=[dict(provider_id='demo',name='研究模型',model='demo-model',api_base='https://example.invalid/v1',temperature=0.3,api_protocol='chat',pricing_mode='manual',input_price_per_m=1,output_price_per_m=2,secrets={'api_key':{'configured':True,'value':'demo-key-not-real'}},api_key_configured=True),dict(provider_id='jev',name='Jev 评分',model='jev-1.13.0',api_base='https://api.b.ai/v1',api_protocol='decisions',secrets={'api_key':{'configured':False,'value':''}})]
profiles=[dict(profile_id='demo-account',name='模拟账户',exchange='binance',market_type='spot',configured=True,supported_market_types=['spot','swap'],secrets={key:{'configured':True,'value':'demo-not-real'} for key in ['api_key','secret','passphrase']})]
profiles.append({**profiles[0], 'profile_id': 'demo-swap', 'name': '模拟合约账户', 'market_type': 'swap'})
news=dict(id=1,timestamp=now,raw=dict(as_of=now,digest='### 本轮市场观察\n\n市场维持区间震荡，交易所公告与宏观政策是本轮重点。**统一情报已同步到所有任务。**\n\n- 新增：交易所上线与维护公告，关注流动性变化。\n- 持续观察：风险偏好与资金流向，等待价格和成交量确认。',items=[dict(title=title,source=source,url='https://example.com',scoring={'score':score,'confidence':0.9}) for title,source,score in [('律动快讯：市场资金流向与最新事件','BlockBeats',80),('币安公告：交易服务维护通知','Binance',60),('宏观政策与风险资产联动观察','政策动态',75)]],source_health={'blockbeats':{'status':'ok','count':12},'binance':{'status':'pending_configuration','error':'待配置公告凭据'}},candidate_count=45,scored_count=40,filtered_count=3,status='degraded'))
usage={'daily':[],'models':[],'agents':[],'providers':[],'purposes':[],'today':{'cost':0.125,'unpriced_calls':1,'costs_by_currency':{'USD':0.125}},'summary':{'cost':0.125,'unpriced_calls':1,'costs_by_currency':{'USD':0.125}}}
config={'globals':{'enable_scheduler':True,'market_timeframes':['1h','4h','1d'],'news':default_news_settings(),'pricing_sync':default_pricing_sync_settings(),'secrets':{},'polymarket':{}},'agents':agents,'llm_providers':providers,'exchange_profiles':profiles,'options':{'market_timeframes':['15m','1h','4h','1d','1w'],'trading_modes':['MOCK','REAL','SPOT_DCA'],'exchanges':['binance','okx']},'prompts':{'files':[]},'pricing':[]}
config['options']['modes'] = ['MOCK', 'REAL', 'SPOT_DCA']
processing_items = [dict(item_id=str(index), title=f'模拟消息 {index + 1}：市场资金流向与交易所公告', source=['BlockBeats', 'Binance', '宏观政策'][index % 3], status='failed' if index < 34 else 'selected' if index < 54 else 'filtered', score=None if index < 34 else round(90 - (index - 34) * 1.8, 1), cached=index > 70, error='Jev score does not match its probability distribution at the returned precision' if index < 34 else None, run_id='a' * 32) for index in range(80)]
processing = dict(run_id='b' * 32, status='degraded', stage='complete', running=False, enabled=True, started_at=now, finished_at=now, last_success_at=now, elapsed_ms=64320, progress={'completed': 1, 'total': 1, 'percent': 100}, counts={'fetched': 125, 'candidates': 80, 'scored': 46, 'failed': 34, 'cached': 9, 'selected': 20, 'filtered': 26}, items=processing_items, source_health=news['raw']['source_health'], error='34 items could not be scored', trace_status='disabled')


def candles(symbol):
    start = int(datetime.now(timezone.utc).timestamp()) - 72 * 3600
    base = 64000 if symbol.startswith('BTC') else 2800 if symbol.startswith('ETH') else 145
    return [{'time': start + index * 3600, 'open': base * (1 + math.sin(index / 5) * .01), 'close': base * (1 + math.sin((index + 1) / 5) * .01), 'high': base * (1.005 + max(math.sin(index / 5), math.sin((index + 1) / 5)) * .01), 'low': base * (.995 + min(math.sin(index / 5), math.sin((index + 1) / 5)) * .01), 'volume': 100 + index} for index in range(72)]


def catalog(request):
    query = request.query_params
    market_type = query.get('market_type', 'spot')
    entries = [dict(symbol=f'{base}/{quote}' + (f':{quote}' if market_type == 'swap' else ''), base=base, quote=quote, display_name=f'{base}/{quote}' + (' 永续' if market_type == 'swap' else ''), market_type=market_type) for base in ['BTC', 'ETH', 'SOL'] + [f'TEST{i:02}' for i in range(100)] for quote in ['USDT', 'USDC']]
    selected = query.get('symbols', '').split(',')
    visible = [item for item in entries if query.get('keyword', query.get('query', '')).upper() in item['symbol'] and (not query.get('quote') or item['quote'] == query['quote'])]
    offset, limit = int(query.get('offset', 0)), int(query.get('limit', 50))
    return dict(symbols=visible[offset:offset + limit], selected_symbols=[item for item in entries if item['symbol'] in selected], invalid_symbols=[item for item in selected if item and item not in {entry['symbol'] for entry in entries}], total=len(visible), has_more=offset + limit < len(visible), quote_currencies=['USDT', 'USDC'])
sessions=[]
@app.api_route('/api/{path:path}',methods=['GET','POST','PUT','DELETE'])
async def fixtures(path:str,request:Request):
    if path=='setup/status': return {'required':False}
    if path=='auth/login': return {'token':'synthetic-ui-token','user':{'name':'UI test'}}
    if path=='auth/me': return {'user':{'name':'UI test'}}
    if path=='config': return config
    if path in {'config/market-symbols', 'chat/market-symbols'}: return catalog(request)
    if path=='public/dashboard': return {'agent_summaries':agents,'compare_candidates':agents,'default_compare_ids':[a['config_id'] for a in agents],'overview_metrics':{'enabled_count':3,'agent_count':3,'runs_24h':{'finished':36,'failed':1,'running':0}},'usage_today':usage['today'],'news_snapshot':news,'symbols':[a['symbol'] for a in agents]}
    if path=='public/compare': return {'series':[],'rows':[]}
    if path.startswith('public/workspace/'):
        agent=next(a for a in agents if a['config_id']==path.split('/')[-1]); timeframe=request.query_params.get('timeframe','1h')
        return {'agent':agent,'market_timeframes':['1h','4h','1d'],'timeframe':timeframe,'position':{'positions':[],'mode':agent['mode'],'balance':1000,'margin_balance':1000},'orders':{'orders':[]},'short_memories':{'short_memories':[{'id':1,'config_id':agent['config_id'],'bucket_start':now,'bucket_end':now,'market_summary':'当前处于区间结构，保持单一主策略；下一轮验证突破量能。','source_count':4}]},'kline':{'symbol':agent['symbol'],'timeframe':timeframe,'candles':candles(agent['symbol']),'positions':[],'pending_orders':[]}}
    if path=='public/usage': return usage
    if 'short-memories' in path: return {'short_memories':[]}
    if path=='news/status': return processing
    if path=='news/runs': return {'runs': [{key: value for key, value in processing.items() if key != 'items'}], 'total': 1}
    if path.startswith('news/runs/'): return {'run': processing}
    if path=='news/scorer-models': return {'models': [{'id': 'jev-latest', 'name': 'jev-latest'}]}
    if path=='news/refresh': return {**processing, 'started': True}
    if path.startswith('agent-runs/'): return {'run': {'run_id': 'a' * 32, 'model': 'jev-1.13.0', 'status': 'error', 'started_at': now, 'messages': [{'role': 'user', 'content': 'Synthetic news scoring request'}], 'error': 'Synthetic score validation error', 'output': '{"score": 2.37, "weighted_score": 1.1}'}}
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
