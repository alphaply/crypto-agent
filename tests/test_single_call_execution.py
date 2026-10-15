"""The model executes once; exact evidence continues through internal services."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp import FastMCP

from backend import database
from backend.agent import agent_tools, tool_registry
from backend.config import config
from backend.mcp import canonical_tools, service
from backend.mcp.tools import register_tools
from backend.mcp.paper_tools import register_paper_tools
from backend.utils.trade_operations import run_once
from backend.utils.trade_summary import compact_trade_receipt, receipt_bytes
from test_exclusive_position_management import ExclusiveExchange, make_service
from test_unified_trading import ledger


FIXTURE = Path(__file__).parent / 'fixtures/legacy_conditional_ack_receipt.json'


def test_sanitized_case_retains_fixed_fingerprints_and_the_original_context_conflict():
    raw=json.loads(FIXTURE.read_text(encoding='utf-8'))
    case=json.loads(FIXTURE.with_name('legacy_conditional_ack_context.json').read_text(encoding='utf-8'))
    for key,expected in case['canonical_receipt_sha256'].items():
        encoded=json.dumps(raw[key],sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')
        assert hashlib.sha256(encoded).hexdigest()==expected
    conflict=case['old_query_failure']
    assert conflict['arguments']['config_id']==raw['current']['continuation']['scope']['account_scope']
    assert conflict['error']=='Error: Conflicting trusted context: config_id'
    assert len(raw['original']['continuation']['previous_assessment']['rules'])==11
    assert raw['current']['requested_amount']==.03 and raw['current']['filled_amount']==0
    assert raw['original']['position']['last_known_amount']==.06
    assert raw['current']['status']=='submitted' and raw['current']['order_status']=='waiting_trigger'


@pytest.mark.parametrize('mode', ['REAL', 'STRATEGY', 'SPOT_DCA'])
def test_default_trading_role_has_only_execution(mode):
    names = [tool.name for tool in tool_registry.get_trade_tools_for_mode(mode)]
    assert names == ['execute_trade_actions']
    assert not {'get_trade_operation', 'get_operation'} & tool_registry._TOOL_BY_NAME.keys()


@pytest.mark.parametrize('name', ['get_trade_operation', 'get_operation', 'get_paper_operation'])
def test_cached_query_name_cannot_execute_or_select_account(ledger, monkeypatch, name):
    query = Mock(side_effect=AssertionError('Retired tools must never reach a query'))
    monkeypatch.setattr('backend.agent.unified_trading.query_unified_operation', query)
    outcome = json.loads(tool_registry.run_trade_tool(name, {
        'target_operation_id': 'offline-close-v2-0001', 'config_id': 'a' * 64, 'symbol': 'ETH/USDT:USDT',
    }, 'trusted-task', 'ETH/USDT:USDT'))
    assert outcome['error_code'] == 'tool_removed'
    query.assert_not_called()


@pytest.mark.parametrize('kind,scopes,count', [('live', ['read','trade','cancel'],7), ('live',['read'],6),
                                             ('paper',['read','trade'],10), ('paper',['read'],7)])
def test_sdk_catalog_and_cached_registration_remove_operation_queries(ledger, monkeypatch, kind, scopes, count):
    caller = {'id': 'offline-key', 'key_type': kind, 'scopes': scopes, 'profile_ids': ['swap'], 'paper_namespace': 'offline'}
    monkeypatch.setattr(service, 'principal', lambda: caller)
    monkeypatch.setattr(service,'list_profiles',lambda _:[{'profile_id':'swap'}])
    monkeypatch.setattr('backend.mcp.settings.get_runtime_profile',lambda _:{'mode':'REAL','market_type':'swap','exit_mode':'independent_exits'})
    server = FastMCP('Single call offline')
    register_tools(server)
    register_paper_tools(server)
    published = {tool.name for tool in asyncio.run(server.list_tools())}
    assert len(published) == count
    for name in ('get_operation','get_trade_operation','get_paper_operation'):
        assert name not in published
        assert server._tool_manager.get_tool(name) is None


def test_noisy_legacy_receipt_keeps_ack_fill_and_previous_position_separate():
    raw = json.loads(FIXTURE.read_text(encoding='utf-8'))
    before = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    for value in raw.values():
        summary = compact_trade_receipt(value)
        assert receipt_bytes(summary) <= 4096
        assert summary['status'] == 'submitted'
        assert summary['filled_amount'] == 0
        assert summary['requested_amount'] == .03
        assert summary['order_id'] == 'algo:offline-algo-0001'
        assert summary['client_id'] == 'cai-offline-original-client'
        assert 'previous_assessment' not in json.dumps(summary)
        assert 'get_trade_operation' not in json.dumps(summary)
    assert compact_trade_receipt(raw['current'])['order_status'] == 'waiting_trigger'
    assert raw['original']['position']['amount'] is None
    assert raw['original']['position']['last_known_amount'] == .06
    assert json.dumps(raw, ensure_ascii=False, sort_keys=True) == before


def test_twenty_noisy_results_fit_actual_utf8_json_and_keep_every_index():
    original = json.loads(FIXTURE.read_text(encoding='utf-8'))['original']
    children = []
    for index in range(20):
        result = deepcopy(original)
        result.update(contract_version=3, operation_id=f'offline-parent:{index}')
        status = ('submitted','unknown','failed','not_executed')[index % 4]
        result['status'] = status
        children.append({'index':index,'action':'close','operation_id':result['operation_id'],'status':status,'result':result})
    raw = {'contract_version':3,'receipt_version':1,'operation_id':'offline-parent','status':'unknown','results':children,
           'account_summary': {'status':'completed','cash':50,'positions':[original]*100}}
    before = deepcopy(raw)
    summary = compact_trade_receipt(raw)
    assert receipt_bytes(summary) <= 32768
    assert len(json.dumps(summary,ensure_ascii=False).encode('utf-8')) <= 32768
    assert [item['index'] for item in summary['results']] == list(range(20))
    assert [item['status'] for item in summary['results']] == [item['status'] for item in children]
    assert [item['operation_id'] for item in summary['results']] == [item['operation_id'] for item in children]
    assert json.dumps(summary).count('account_summary') == 1
    assert raw == before


def test_summary_never_replaces_durable_raw_or_request_hash(ledger):
    original = json.loads(FIXTURE.read_text(encoding='utf-8'))['original']
    request = {'contract_version':2,'tool':'close','args':{'side':'SHORT','amount':.03,'order_type':'take_profit_market','trigger_price':2380}}
    raw = run_once('offline-task','ETH/USDT:USDT','offline-close-v2-0001',request,lambda: deepcopy(original))
    compact_trade_receipt(raw)
    with database.get_db_conn() as conn:
        row = conn.execute('SELECT result,request_hash FROM trade_action_runs WHERE config_id=%s',('offline-task',)).fetchone()
        assert json.loads(row['result']) == original
        assert row['request_hash'] == hashlib.sha256(json.dumps(request,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def test_paper_order_and_each_protection_identity_survive_default_projection():
    order = {'order_id':'paper-limit','state':'open','market_type':'swap','symbol':'ETH/USDT:USDT',
             'exchange':'binance','position_side':'SHORT','amount':.03,'price':2380,
             'order_type':'limit','close_all':False}
    raw = {'status':'submitted','operation_id':'offline-paper-place','order':order,
           'account':{'positions':[{'legacy_private':'not needed'}]*200}}
    projected = compact_trade_receipt(raw)
    assert projected['order_id'] == 'paper-limit' and projected['order_status'] == 'open'
    assert projected['requested_amount'] == .03 and projected['price'] == 2380
    assert projected['quantity_unit'] == 'base' and projected['side'] == 'SHORT'
    assert receipt_bytes(projected) <= 4096 and 'account' not in projected
    protections = [{**order,'order_id':'paper-sl','order_type':'stop_loss','trigger_price':2600},
                   {**order,'order_id':'paper-tp','order_type':'take_profit','trigger_price':2200}]
    for name in ('orders','protection_orders'):
        result = compact_trade_receipt({'status':'submitted','operation_id':'offline-paper-protect',name:protections})
        assert [item['order_id'] for item in result[name]] == ['paper-sl','paper-tp']
        assert [item['order_status'] for item in result[name]] == ['open','open']
        assert [item['trigger_price'] for item in result[name]] == [2600,2200]
        assert receipt_bytes(result) <= 4096
    fill = compact_trade_receipt({'status':'completed','action':'open','fill':{
        'action':'open_short','amount':.03,'symbol':'ETH/USDT:USDT','price':2380,'fee':.03,'observed_at':123}})
    assert fill['action'] == 'open' and fill['filled_amount'] == .03 and fill['observed_at'] == 123


class AckThenReadsFail(ExclusiveExchange):
    def __init__(self):
        super().__init__()
        self.ack_received = False
        self.quantities = {'LONG':0,'SHORT':.06}

    def create_order(self,*args,**kwargs):
        response = super().create_order(*args,**kwargs)
        self.ack_received = True
        return response

    def fetch_order(self,*args,**kwargs):
        assert not self.ack_received, 'No mandatory fetch_order after accepted financial ACK'
        return super().fetch_order(*args,**kwargs)

    def fetch_positions(self,*args,**kwargs):
        assert not self.ack_received, 'No mandatory fetch_positions after accepted financial ACK'
        return super().fetch_positions(*args,**kwargs)


def test_v2_conditional_close_returns_ack_without_post_submission_read(ledger,monkeypatch):
    venue = AckThenReadsFail()
    executor = make_service(venue,'offline-task',side='SHORT',policy='visible_first_claim')
    cfg = {**executor.mt.runtime_config,'enabled':True}
    executor.mt.runtime_config = cfg
    monkeypatch.setattr(config,'get_config_by_id',lambda _:cfg)
    monkeypatch.setattr(agent_tools,'MarketTool',lambda **_:executor.mt)
    payload = {'side':'SHORT','order_type':'take_profit_market','amount':.03,'trigger_price':2380,'reason':'stage exit'}
    result = json.loads(tool_registry.run_trade_tool('close',payload,'offline-task','ETH/USDT:USDT','offline-close-v2-0001'))
    assert result['status'] == 'submitted',result.get('error') or result
    assert result['filled_amount'] == 0
    assert result['requested_amount'] == .03
    assert venue.ack_received
    assert len(venue.writes) == 1
    assert 'get_trade_operation' not in json.dumps(result)


@pytest.mark.parametrize('action',['open','close','edit','cancel','protect'])
def test_attached_real_actions_return_authoritative_ack_without_postread(ledger,monkeypatch,action):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from test_position_protection import Exchange,entry
    from backend.utils.position_protection import PositionProtection
    class AckGuard(Exchange):
        armed=False
        acknowledged=False
        postreads=[]
        def create_order(self,*args,**kwargs):
            result=super().create_order(*args,**kwargs)
            result.update(symbol=args[0],amount=args[3],price=args[4])
            if self.armed:self.acknowledged=True
            return result
        def cancel_order(self,*args,**kwargs):
            result=super().cancel_order(*args,**kwargs)
            if self.armed:self.acknowledged=True
            return result
        def edit_order(self,identity,symbol,kind,side,amount,price):
            self.orders[identity].update(amount=amount,price=price)
            if self.armed:self.acknowledged=True
            return dict(self.orders[identity])
        def fetch_order(self,*args,**kwargs):
            if self.acknowledged:self.postreads.append('order')
            assert not self.acknowledged,'Accepted ACK must return before another order GET'
            return super().fetch_order(*args,**kwargs)
        def fetch_positions(self,*args,**kwargs):
            if self.acknowledged:self.postreads.append('position')
            assert not self.acknowledged,'Accepted ACK must return before another position GET'
            return super().fetch_positions(*args,**kwargs)
    exchange=AckGuard();exchange.postreads=[]
    cfg={'config_id':'cfg','mode':'REAL','exit_mode':'attached_optional','symbol':'ETH/USDT'}
    monkeypatch.setattr(config,'get_config_by_id',lambda _:cfg)
    mt=SimpleNamespace(exchange=exchange,config_id='cfg',runtime_config=cfg)
    monkeypatch.setattr(agent_tools,'MarketTool',lambda **_:mt)
    @contextmanager
    def owned(*args,**kwargs):
        yield {}
    monkeypatch.setattr('backend.utils.position_management.management_action',owned)
    values={'action':action,'reason':'offline requested action'}
    if action in {'edit','cancel'}:
        opened=PositionProtection(mt).open('ETH/USDT',entry(),operation_id='preexisting-entry')
        values['order_id']='regular:'+str(opened['id'])
    if action=='open':values.update(side='LONG',order_type='limit',amount=1,price=100)
    if action=='close':exchange.quantity=1;values.update(side='LONG',order_type='market',close_all=True)
    if action=='edit':values.update(remaining_amount=.3,price=101)
    if action=='protect':exchange.quantity=1;values.update(side='LONG',stop_loss=90,take_profit=120)
    exchange.armed=True
    result=json.loads(tool_registry.run_trade_tool('execute_trade_actions',{'contract_version':3,'actions':[values]},
                                                 'cfg','ETH/USDT','ack-'+action))
    assert exchange.acknowledged and not exchange.postreads,result
    assert result['status']==('submitted' if action in {'open','protect'} else 'completed'),result
    if action=='protect':
        assert result['results'][0]['result']['order_status']=='waiting_trigger'
        assert result['results'][0]['result']['filled_amount']==0
