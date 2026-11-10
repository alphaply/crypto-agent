"""Accepted task orders, independent of model tool-call attempts."""
import json

from backend import database
from backend.utils.task_performance import stamp_ms


def task_orders(config_id: str, symbols: list[str], mode: str) -> list[dict]:
    def item(symbol, identity, status, created, observed=None):
        return dict(symbol=symbol, order_id=str(identity), status=status,
                    timestamp_ms=stamp_ms(created), observed_at_ms=stamp_ms(observed) or 0)

    with database.get_db_conn() as conn:
        if mode == 'STRATEGY':
            entries = conn.execute('SELECT * FROM mock_orders WHERE config_id=%s AND symbol=ANY(%s)',
                                   (config_id, symbols)).fetchall()
            exits = conn.execute('SELECT * FROM mock_exit_orders WHERE config_id=%s AND symbol=ANY(%s)',
                                 (config_id, symbols)).fetchall()
            return ([item(r['symbol'], r['order_id'], r['status'], r['timestamp']) for r in entries] +
                    [item(r['symbol'], r['order_id'], r['status'], r['created_at']) for r in exits])
        if mode == 'SPOT_DCA':
            rows = conn.execute('''SELECT o.*, f.status AS sync_status FROM orders o
                LEFT JOIN spot_order_fills f USING(config_id,symbol,order_id)
                WHERE o.config_id=%s AND o.symbol=ANY(%s) AND o.trade_mode='SPOT_DCA'
                  AND COALESCE(o.event_type,'ORDER_CREATED')='ORDER_CREATED' ''', (config_id,symbols)).fetchall()
            result = [item(r['symbol'], r['order_id'], r['sync_status'] or r['status'], r['timestamp']) for r in rows]
            originals = {(r['symbol'],str(r['order_id'])):r for r in result}
            # The v3 cancel ACK is durable before the periodic order-fill sync.
            # Count only its explicit cancelled order status, never a successful
            # tool status for an order that had already filled.
            receipts = conn.execute('''SELECT i.symbol,i.target_id,r.result,r.updated_at
                FROM spot_management_intents i JOIN trade_action_runs r USING(config_id,operation_id)
                WHERE i.config_id=%s AND i.symbol=ANY(%s) AND i.action='cancel' ''',
                (config_id,symbols)).fetchall()
            for receipt in receipts:
                original = originals.get((receipt['symbol'],receipt['target_id']))
                evidence = json.loads(receipt['result'] or '{}')
                if original and evidence.get('order_status') in {'canceled','cancelled'}:
                    result.append({**original,'status':'cancelled','observed_at_ms':stamp_ms(receipt['updated_at'])})
        else:
            result = []
        from backend.utils.trade_evidence import configured_account_scope
        if mode == 'REAL':
            scope = configured_account_scope(config_id)
        else:
            from backend.mcp.spot_orders import _configured_account_scope
            scope = _configured_account_scope(config_id)
        if mode == 'REAL':
            links = conn.execute('''SELECT symbol,order_id,payload FROM execution_order_links
                WHERE config_id=%s AND account_scope=%s
                AND (symbol=ANY(%s) OR split_part(symbol,':',1)=ANY(%s))''',
                (config_id,scope,symbols,[s for s in symbols if ':' not in s])).fetchall()
            # A linked exchange ID proves acceptance, even if an old creation
            # timestamp is missing. Unknown dates belong only to all history.
            result += [item(r['symbol'], r['order_id'], 'accepted', None) for r in links
                       if json.loads(r['payload']).get('source','agent') == 'agent']
            plans = conn.execute('''SELECT payload FROM trade_cycle_evidence WHERE config_id=%s AND account_scope=%s
                AND (symbol=ANY(%s) OR split_part(symbol,':',1)=ANY(%s))''',
                (config_id,scope,symbols,[s for s in symbols if ':' not in s])).fetchall()
            children = {}
            for row in plans:
                plan = json.loads(row['payload'])
                for record in plan.get('entries', []) + plan.get('exits', []):
                    if not record.get('id') or record.get('source', 'agent') != 'agent':
                        continue
                    from backend.utils.exit_orders import order_reference
                    identity = order_reference(record, current=False)
                    # Regular ledger IDs are bare; conditional parents retain
                    # their namespace so an unrelated numeric ID cannot collide.
                    identity = identity.removeprefix('regular:')
                    child = record.get('execution_order_id')
                    if child:
                        children[(plan['symbol'], str(child))] = identity
                    status = record.get('status')
                    result.append(item(plan['symbol'], identity, 'accepted' if status in {'unknown','submitting',None} else status,
                                       record.get('created_at'), plan.get('updated_at')))
            result = [r for r in result if (r['symbol'],r['order_id']) not in children]
        else:
            rows = conn.execute('''SELECT * FROM mcp_spot_exits WHERE config_id=%s AND account_scope=%s
                AND symbol=ANY(%s)''', (config_id,scope,symbols)).fetchall()
            result += [item(r['symbol'],'spot_exit:'+r['exit_id'],r['state'],r['created_at'],r['updated_at']) for r in rows]
    return result
