"""Display durable automatic executions beside model requests, with no exchange writes."""
from datetime import datetime, timezone
import hashlib
import json


def record_automatic_exit(conn, config_id: str, symbol: str, identity: str, *, scope: str,
                          role: str, result: dict, when: float,
                          time_source: str = 'observation') -> None:
    key = hashlib.sha256(f'{scope}\0{symbol}\0{identity}'.encode()).hexdigest()
    operation_id = 'auto:' + key
    timestamp = datetime.fromtimestamp(when, timezone.utc).isoformat()
    args = dict(source='automatic', trigger_kind=role, time_source=time_source)
    result = dict(result, action='close', symbol=symbol, source='automatic', trigger_kind=role)
    conn.execute('''INSERT INTO agent_trade_calls
        (config_id,operation_id,tool_name,symbol,action,arguments_json,result_json,status,created_at,updated_at)
        VALUES(%s,%s,'automatic_exit',%s,'close',%s,%s,%s,%s,%s)
        ON CONFLICT(config_id,operation_id) DO UPDATE SET result_json=excluded.result_json,
          status=excluded.status,updated_at=excluded.updated_at
        WHERE agent_trade_calls.tool_name='automatic_exit'
          AND COALESCE((agent_trade_calls.result_json::jsonb->>'filled_amount')::float,0)
              <= COALESCE((excluded.result_json::jsonb->>'filled_amount')::float,0)
          AND (agent_trade_calls.status NOT IN ('filled','partially_filled')
               OR excluded.status IN ('filled','partially_filled'))
          AND (agent_trade_calls.status<>'filled' OR excluded.status='filled')''',
        (config_id,operation_id,symbol,json.dumps(args),json.dumps(result),result['status'],timestamp,timestamp))


def record_plan_execution_events(conn, config_id: str, plan: dict) -> None:
    from backend.utils.exit_orders import exit_group, order_reference
    factor = float(plan.get('contract_size') or 1)
    for record in plan.get('legs', []) + plan.get('exits', []):
        role = {'sl':'stop_loss','tp':'take_profit'}.get(record.get('kind')) or exit_group(record)
        if role not in {'stop_loss','take_profit'} or not record.get('id') or record.get('source','agent') != 'agent':
            continue
        filled = float(record.get('filled') or 0) * factor
        verified = record.get('fill_verified', filled > 0) and not record.get('fill_pending')
        if not (record.get('triggered') or record.get('execution_order_id') or verified and filled > 0):
            continue
        status = ('filled' if record.get('status') in {'filled','closed'} else 'partially_filled') if verified and filled > 0 else 'pending'
        parent = order_reference(record, current=False)
        result = dict(status=status, order_id=order_reference(record), parent_order_id=parent,
                      side=plan['side'], trigger_price=record.get('trigger_price'),
                      order_type=record.get('order_type') or record.get('exit_type'),
                      amount=float(record['amount'])*factor if record.get('amount') is not None else None,
                      filled_amount=filled if verified else None,
                      order_status=record.get('status'), triggered=True,
                      message='止损自动触发' if role=='stop_loss' else '止盈自动触发')
        when = record.get('triggered_at') or plan.get('updated_at')
        if when:
            record_automatic_exit(conn,config_id,plan['symbol'],parent,scope=plan['account_scope'],
                                  role=role,result=result,when=when,
                                  time_source='exchange' if record.get('triggered_at') else 'observation')


def backfill_automatic_exits(conn, config_id: str) -> None:
    """Old evidence is already durable; index it without inventing tool calls."""
    from backend.config import config
    cfg = config.get_config_by_id(config_id)
    if not cfg:
        return
    from backend.utils.spot_portfolio import get_config_symbols
    symbols = get_config_symbols(cfg)
    mode = cfg.get('mode','STRATEGY')
    if mode == 'REAL':
        from backend.utils.trade_evidence import configured_account_scope
        try:
            scope = configured_account_scope(config_id)
        except ValueError:
            # Existing indexed history remains readable after credentials are
            # removed. Never attribute extra evidence to an unverified account.
            return
        for row in conn.execute('''SELECT payload FROM real_protection_plans WHERE config_id=%s
                AND (symbol=ANY(%s) OR split_part(symbol,':',1)=ANY(%s))
            UNION SELECT payload FROM trade_cycle_evidence WHERE config_id=%s AND account_scope=%s
                AND (symbol=ANY(%s) OR split_part(symbol,':',1)=ANY(%s))''',
            (config_id,symbols,[s for s in symbols if ':' not in s],config_id,scope,symbols,[s for s in symbols if ':' not in s])).fetchall():
            plan = json.loads(row['payload'])
            if plan.get('account_scope') == scope:
                record_plan_execution_events(conn,config_id,plan)
        # Historical fills can outlive the mutable plan. Keep those executions
        # visible too, matching an existing parent event before creating a row.
        rows = conn.execute('''SELECT f.symbol,f.order_id,f.timestamp_ms,f.payload,l.role,l.payload AS metadata
            FROM execution_fills f JOIN execution_order_links l USING(account_scope,symbol,order_id)
            WHERE f.config_id=%s AND l.config_id=%s AND f.account_scope=%s
              AND l.role IN ('stop_loss','take_profit')
              AND (f.symbol=ANY(%s) OR split_part(f.symbol,':',1)=ANY(%s))
            ORDER BY f.timestamp_ms''',(config_id,config_id,scope,symbols,[s for s in symbols if ':' not in s])).fetchall()
        grouped = {}
        for row in rows:
            fill, metadata = json.loads(row['payload']), json.loads(row['metadata'])
            amount = fill.get('base_amount',fill.get('amount'))
            if not isinstance(amount,(float,int)) or amount <= 0:
                continue
            event = grouped.setdefault((row['symbol'],row['order_id']),dict(amount=0.0,cost=0.0,when=row['timestamp_ms'],row=row,metadata=metadata))
            event['amount'] += amount
            event['cost'] += amount * (fill.get('price') or 0)
        for (symbol,order_id), event in grouped.items():
            existing = conn.execute('''SELECT operation_id,result_json,status FROM agent_trade_calls WHERE config_id=%s
                AND symbol=%s AND tool_name='automatic_exit' AND result_json::jsonb->>'order_id' IN (%s,%s)''',
                (config_id,symbol,order_id,'regular:'+order_id)).fetchone()
            if existing:
                receipt = json.loads(existing['result_json'])
                if event['amount'] >= (receipt.get('filled_amount') or 0):
                    complete = receipt.get('order_status') in {'closed','filled'} or (
                        receipt.get('amount') is not None and event['amount'] >= receipt['amount']-1e-10)
                    state = 'filled' if complete or existing['status']=='filled' else 'partially_filled'
                    receipt.update(filled_amount=event['amount'],average_price=event['cost']/event['amount'],status=state)
                    conn.execute('UPDATE agent_trade_calls SET result_json=%s,status=%s WHERE config_id=%s AND operation_id=%s',
                                 (json.dumps(receipt),state,config_id,existing['operation_id']))
                continue
            metadata, row = event['metadata'],event['row']
            identity = metadata.get('parent_order_id') or 'regular:'+order_id
            record_automatic_exit(conn,config_id,symbol,identity,scope=scope,role=row['role'],
                when=event['when']/1000,time_source='execution',result=dict(status='filled',
                order_id='regular:'+order_id,side=metadata.get('side'),trigger_price=metadata.get('trigger_price'),
                filled_amount=event['amount'],average_price=event['cost']/event['amount']))
    elif mode == 'STRATEGY':
        from backend.utils.task_performance import stamp_ms
        for row in conn.execute('''SELECT * FROM mock_exit_orders WHERE config_id=%s
            AND symbol=ANY(%s) AND triggered_at IS NOT NULL''', (config_id,symbols)).fetchall():
            role = 'stop_loss' if row['order_type'].startswith('stop_') else 'take_profit'
            filled = row['filled_amount']
            record_automatic_exit(conn,config_id,row['symbol'],row['order_id'],scope='strategy',role=role,
                when=stamp_ms(row['triggered_at'])/1000,time_source='execution',result=dict(
                status='filled' if filled and row['status']=='CLOSED' else 'partially_filled' if filled else 'pending',
                order_id=row['execution_order_id'] or row['order_id'], parent_order_id=row['order_id'],
                side=row['pos_side'],order_type=row['order_type'],trigger_price=row['trigger_price'],
                amount=row['amount'],filled_amount=filled,triggered=True))
        for row in conn.execute("""SELECT * FROM orders WHERE config_id=%s AND symbol=ANY(%s)
            AND trade_mode='STRATEGY' AND event_type IN ('SL_HIT','TP_HIT') AND is_auto=1""",
            (config_id,symbols)).fetchall():
            when = stamp_ms(row['timestamp'])
            if when is None:
                continue
            role = 'stop_loss' if row['event_type']=='SL_HIT' else 'take_profit'
            # Independent exits already have a stable parent entry above.
            if row['parent_order_id'] and conn.execute('SELECT 1 FROM mock_exit_orders WHERE config_id=%s AND order_id=%s',
                                                      (config_id,row['parent_order_id'])).fetchone():
                continue
            record_automatic_exit(conn,config_id,row['symbol'],row['order_id'],scope='strategy',role=role,
                when=when/1000,time_source='execution',result=dict(status='filled',order_id=row['order_id'],
                side='SHORT' if 'SHORT' in row['side'] else 'LONG', amount=row['amount'],
                filled_amount=row['amount'],average_price=row['entry_price'],realized_pnl=row['realized_pnl']))
    elif mode == 'SPOT_DCA':
        from backend.mcp.spot_orders import _configured_account_scope
        scope = _configured_account_scope(config_id)
        for row in conn.execute('''SELECT * FROM mcp_spot_exits WHERE config_id=%s
            AND account_scope=%s AND symbol=ANY(%s) AND kind IN ('stop_loss','take_profit','oco')''',
            (config_id,scope,symbols)).fetchall():
            metadata = json.loads(row['payload'])
            if not metadata.get('fill_verified') or row['filled'] <= 0:
                continue
            request = metadata.get('request') or {}
            record_automatic_exit(conn,config_id,row['symbol'],'spot_exit:'+row['exit_id'],scope=scope,
                role=row['kind'],when=metadata.get('fill_observed_at') or row['updated_at'],
                result=dict(status='filled' if row['state']=='filled' else 'partially_filled',
                order_id='spot_exit:'+row['exit_id'],side='LONG',order_type=row['kind'],
                amount=row['amount'],filled_amount=row['filled'],
                stop_loss=request.get('stop_loss'),take_profit=request.get('take_profit')))
