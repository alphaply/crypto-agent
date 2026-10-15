"""Streaming cycle reconstruction with explicit limits and complete-result publication."""
from contextlib import nullcontext, ExitStack
from datetime import datetime
import hashlib
import json

from backend.database import get_db_conn, TZ_CN
from backend.utils.runtime_capacity import positive_setting, CapacityBusy, capacity_limits_enabled
from backend.utils.sqlite_blob import bounded_text


def _rows(config_id, end_ms, scope, symbol, maximum, read_conn):
    with (nullcontext(read_conn) if read_conn is not None else get_db_conn()) as conn:
        records = conn.execute(
            'SELECT f.rowid AS fill_rowid,l.rowid AS link_rowid,'
            'f.account_scope,f.symbol,f.trade_id,f.order_id,f.config_id,f.timestamp_ms,l.role '
            'FROM execution_fills f LEFT JOIN execution_order_links l '
            'ON f.account_scope=l.account_scope AND f.symbol=l.symbol AND f.order_id=l.order_id '
            'WHERE EXISTS (SELECT 1 FROM execution_order_links own WHERE own.config_id=? '
            'AND own.account_scope=f.account_scope AND own.symbol=f.symbol) AND f.timestamp_ms<? '
            'AND (? IS NULL OR f.account_scope=?) AND (? IS NULL OR f.symbol=?) '
            'ORDER BY f.timestamp_ms,LENGTH(f.trade_id),f.trade_id',
            (config_id,end_ms,scope,scope,symbol,symbol))
        for record in records:
            row = dict(record)
            with ExitStack() as stack:
                try:
                    row['payload'] = stack.enter_context(bounded_text(
                        conn,'execution_fills','payload',row['fill_rowid'],limit=maximum))
                    row['link_metadata'] = stack.enter_context(bounded_text(
                        conn,'execution_order_links','payload',row['link_rowid'],limit=maximum)) if row['link_rowid'] is not None else None
                    row['payload_bytes'] = len(row['payload'].encode('utf-8'))
                    row['metadata_bytes'] = len((row['link_metadata'] or '').encode('utf-8'))
                except CapacityBusy as exc:
                    if exc.reason != 'record_bytes':
                        raise
                    row.update(payload=None,payload_bytes=maximum+1,metadata_bytes=0)
                yield row


def _protection(cycle, config_id, maximum, read_conn):
    limits_enabled = capacity_limits_enabled()
    revisions, used = {}, 0
    with (nullcontext(read_conn) if read_conn is not None else get_db_conn()) as conn:
        rows=conn.execute('SELECT rowid AS audit_rowid,timestamp FROM real_protection_events WHERE config_id=? ORDER BY id', (config_id,))
        for audit in rows:
            try:
                with bounded_text(conn,'real_protection_events','payload',audit['audit_rowid'],limit=maximum) as payload:
                    plan=json.loads(payload)
            except CapacityBusy as exc:
                if exc.reason!='record_bytes':
                    raise
                return False
            if (plan.get('account_scope'),plan.get('symbol'),plan.get('side')) != (
                    cycle['account_scope'],cycle['symbol'],cycle['side']):
                continue
            if cycle.get('plan_episode_id') and plan.get('episode_id') != cycle['plan_episode_id']:
                continue
            ts=int(TZ_CN.localize(datetime.fromisoformat(audit['timestamp'])).timestamp()*1000)
            if ts>cycle['closed_at_ms']:
                continue
            revision=plan.get('revision',0)
            if revision not in revisions:
                item={'timestamp_ms':ts,'revision':revision,'stop_loss':plan.get('stop_loss'),'take_profit':plan.get('take_profit')}
                used+=len(json.dumps(item).encode('utf-8'))+128
                if limits_enabled and used>maximum:
                    return False
                revisions[revision]=item
    timeline=sorted(revisions.values(),key=lambda item:item['timestamp_ms'])
    before=[item for item in timeline if item['timestamp_ms']<=cycle['opened_at_ms']]
    cycle['protection_history']=before[-1:]+[item for item in timeline if item['timestamp_ms']>cycle['opened_at_ms']]
    if cycle['protection_history']:
        final=cycle['protection_history'][-1]
        cycle.update(stop_loss=final['stop_loss'],take_profit=final['take_profit'])
    return True


def reconstruct_cycles(config_id,start_ms,end_ms,scope=None,symbol=None,*,cycle_sink=None,read_conn=None):
    from backend.utils.execution_ledger import number
    maximum = positive_setting('RUNTIME_MAX_RECORD_BYTES',4*1024*1024)
    limits_enabled = capacity_limits_enabled()
    rows = _rows(config_id,end_ms,scope,symbol,maximum,read_conn)
    current = {}
    work_sizes, current_bytes, output_bytes = {}, 0, 0
    completed_count, excluded_count, omitted_completed, omitted_excluded = 0, 0, 0, 0
    scan_complete, reason_code = True, None
    last_cursor = None
    completed = []
    excluded = []
    unmatched = 0
    for row in rows:
        last_cursor={'timestamp_ms':row['timestamp_ms'],'trade_id':row['trade_id']}
        if row['payload'] is None or limits_enabled and (row['metadata_bytes'] or 0)>maximum:
            scan_complete,reason_code=False,'cycle_record_budget'
            break
        fill = json.loads(row['payload'])
        metadata = json.loads(row['link_metadata'] or '{}')
        side = str(fill.get('position_side') or metadata.get('side') or '').upper()
        if side == 'BOTH':
            side = metadata.get('side')
        if side not in {'LONG', 'SHORT'}:
            unmatched += 1
            for key in list(current):
                if key[:2] == (row['account_scope'], row['symbol']):
                    current[key]['incomplete'] = True
            continue
        role = row['role']
        if not role:
            # Direction establishes increase/decrease, never the reason for an exit.
            action = str(fill.get('side') or '').lower()
            if action not in {'buy', 'sell'}:
                unmatched += 1
                continue
            role = 'entry' if (action == 'buy') == (side == 'LONG') else 'external_unknown_exit'
        key = (row['account_scope'], row['symbol'], side)
        quantity = number(fill.get('base_amount', fill.get('amount')))
        price = number(fill.get('price'))
        if not quantity or not price:
            unmatched += 1
            continue
        row_work=(row['payload_bytes'] or 0)+(row['metadata_bytes'] or 0)+512
        previous_work=work_sizes.get(key,0)
        if limits_enabled and current_bytes+row_work>maximum:
            scan_complete,reason_code=False,'cycle_working_set_budget'
            break
        work_sizes[key]=previous_work+row_work
        current_bytes+=row_work
        cycle = current.get(key)
        if role == 'entry':
            if not cycle:
                cycle = {
                    'account_scope': row['account_scope'],
                    'config_id': config_id,
                    'symbol': row['symbol'],
                    'side': side,
                    'episode_id': metadata.get('episode_id'),
                    'opened_at_ms': row['timestamp_ms'],
                    'remaining_base': 0,
                    'entered_base': 0,
                    'exited_base': 0,
                    'entry_cost': 0,
                    'exit_cost': 0,
                    'realized_pnl_before_fees': 0,
                    'missing_pnl': False,
                    'missing_fees': False,
                    'fees_by_currency': {},
                    'entry_trade_ids': [],
                    'exit_trade_ids': [],
                    'entry_order_ids': [],
                    'exit_order_ids': [],
                    'exit_reasons': [],
                    'events': [],
                    'incomplete': False,
                    'foreign_entry': False,
                    'owned_entry': False,
                    'take_profit': number(metadata.get('take_profit')),
                    'stop_loss': number(metadata.get('stop_loss')),
                }
                current[key] = cycle
            cycle['owned_entry'] |= row['config_id'] == config_id
            cycle['foreign_entry'] |= row['config_id'] != config_id
            cycle['episode_id'] = cycle.get('episode_id') or metadata.get('episode_id')
            cycle['take_profit'] = number(metadata.get('take_profit')) or cycle.get('take_profit')
            cycle['stop_loss'] = number(metadata.get('stop_loss')) or cycle.get('stop_loss')
            cycle['remaining_base'] += quantity
            cycle['entered_base'] += quantity
            cycle['entry_cost'] += quantity * price
            cycle['entry_trade_ids'].append(row['trade_id'])
            if row['order_id'] and row['order_id'] not in cycle['entry_order_ids']:
                cycle['entry_order_ids'].append(row['order_id'])
        else:
            if not cycle or quantity > cycle['remaining_base'] + 1e-10:
                unmatched += 1
                current.pop(key, None)
                current_bytes-=work_sizes.pop(key,0)
                continue
            cycle['remaining_base'] = max(0, cycle['remaining_base'] - quantity)
            cycle['exited_base'] += quantity
            cycle['exit_cost'] += quantity * price
            if role not in cycle['exit_reasons']:
                cycle['exit_reasons'].append(role)
            cycle['exit_trade_ids'].append(row['trade_id'])
            if row['order_id'] and row['order_id'] not in cycle['exit_order_ids']:
                cycle['exit_order_ids'].append(row['order_id'])
            if role == 'take_profit':
                cycle['take_profit'] = number(metadata.get('trigger_price')) or cycle.get('take_profit')
            elif role == 'stop_loss':
                cycle['stop_loss'] = number(metadata.get('trigger_price')) or cycle.get('stop_loss')
        cycle['events'].append({'trade_id': row['trade_id'], 'order_id': row['order_id'],
                                'timestamp_ms': row['timestamp_ms'], 'role': role,
                                'quantity': quantity, 'price': price,
                                'remaining_base': cycle['remaining_base'],
                                'owner_known': row['config_id'] == config_id})
        pnl = fill.get('realized_pnl')
        if role != 'entry' and pnl is None:
            cycle['missing_pnl'] = True
        elif pnl is not None:
            cycle['realized_pnl_before_fees'] += pnl
        fee_items = fill.get('fees') or ([fill['fee']] if fill.get('fee') else [])
        if not fee_items:
            cycle['missing_fees'] = True
        for fee in fee_items:
            cost = number(fee.get('cost'))
            currency = fee.get('currency')
            if cost is None or not currency:
                cycle['missing_fees'] = True
            else:
                cycle['fees_by_currency'][currency] = cycle['fees_by_currency'].get(currency, 0) + cost
        if cycle['remaining_base'] <= 1e-10:
            cycle['remaining_base'] = 0
            cycle['closed_at_ms'] = row['timestamp_ms']
            cycle['entry_vwap'] = cycle['entry_cost'] / cycle['entered_base']
            cycle['exit_vwap'] = cycle['exit_cost'] / cycle['exited_base']
            cycle['holding_seconds'] = (cycle['closed_at_ms'] - cycle['opened_at_ms']) / 1000
            cycle['plan_episode_id'] = cycle.get('episode_id')
            identity = f"{row['account_scope']}:{row['symbol']}:{side}:{cycle['entry_trade_ids'][0]}"
            cycle['episode_id'] = hashlib.sha256(identity.encode()).hexdigest()
            cycle['add_count'] = max(0, len(cycle['entry_order_ids']) - 1)
            cycle['exit_count'] = len(cycle['exit_order_ids'])
            cycle['final_exit_reason'] = role
            cycle['settlement_currency'] = row['symbol'].split(':')[-1] if ':' in row['symbol'] else row['symbol'].split('/')[-1]
            if cycle['missing_pnl']:
                cycle['realized_pnl_before_fees'] = None
            if cycle['closed_at_ms'] >= start_ms:
                if cycle['owned_entry'] and not cycle['foreign_entry'] and not cycle['incomplete']:
                    if not _protection(cycle,config_id,maximum,read_conn):
                        scan_complete,reason_code=False,'cycle_protection_budget'
                        break
                    encoded_size=len(json.dumps(cycle,ensure_ascii=False,default=str).encode('utf-8'))
                    if limits_enabled and encoded_size>maximum:
                        scan_complete,reason_code=False,'cycle_record_budget'
                        break
                    completed_count+=1
                    if cycle_sink is not None:
                        cycle_sink(cycle)
                    if not limits_enabled or output_bytes+encoded_size<=maximum:
                        completed.append(cycle)
                        output_bytes+=encoded_size
                    else:
                        omitted_completed+=1
                elif cycle['owned_entry'] or row['config_id'] == config_id:
                    excluded_count+=1
                    encoded_size=len(json.dumps(cycle,ensure_ascii=False,default=str).encode('utf-8'))
                    if not limits_enabled or output_bytes+encoded_size<=maximum:
                        excluded.append(cycle)
                        output_bytes+=encoded_size
                    else:
                        omitted_excluded+=1
            current.pop(key, None)
            current_bytes-=work_sizes.pop(key,0)
    return {'completed':completed,'completed_count':completed_count if scan_complete else None,
            'excluded_cycles':excluded,'excluded_cycle_count':excluded_count if scan_complete else None,
            'unmatched_fill_count':unmatched,'open_cycles':[cycle for cycle in current.values() if cycle['owned_entry']],
            'scan_complete':scan_complete,'projection_complete':not (omitted_completed or omitted_excluded),
            'omitted_completed':omitted_completed,'omitted_excluded':omitted_excluded,
            'reason_code':reason_code,'next_cursor':None if scan_complete else last_cursor}
