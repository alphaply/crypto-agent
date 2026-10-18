"""Offline, reproducible cross-round demonstration; no exchange client is used.

Run: uv run python tests/demo_independent_v2.py --output <receipts.json>
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from backend import database
from backend.database.independent import MockIndependentTrading
from backend.database.schema import initialize_schema


def demonstrate():
    previous_database=database.DB_NAME
    events=[]
    try:
        with tempfile.TemporaryDirectory(prefix='crypto-independent-demo-') as directory:
            database.DB_NAME=str(Path(directory)/'isolated-demo.sqlite')
            with database.get_db_conn() as conn:
                initialize_schema(conn)
            store=MockIndependentTrading('offline-demo','ETH/USDT')
            base_time=int((time.time()+120)*1000)

            def record(phase,action,receipt):
                events.append(dict(phase=phase,action=action,receipt=receipt))
                return receipt

            def open_order(side,amount,operation):
                return store.open(SimpleNamespace(
                    action='BUY_LIMIT' if side=='LONG' else 'SELL_LIMIT',amount=amount,
                    entry_price=100,stop_loss=None,take_profit=None,reason='Offline demonstration',
                    valid_duration_hours=24),operation_id=operation,contract_version=2)

            record('round_1','open LONG 0.4',open_order('LONG',.4,'entry-1'))
            record('round_1','open LONG 0.6',open_order('LONG',.6,'entry-2'))
            record('round_1','open SHORT 0.5',open_order('SHORT',.5,'entry-3'))
            assert store.snapshot()['exits']==[]
            record('maintenance','fill entries',store.monitor(100,100,base_time))
            record('round_1','snapshot without required TP/SL',store.snapshot())

            def exit_order(kind,trigger,amount=None,all_mode=False,operation=None):
                return store.close('LONG',amount,kind,100,trigger_price=trigger,close_all=all_mode,
                                   reason='Offline staged exit',operation_id=operation,contract_version=2)

            tp1=record('round_2','TP fixed 0.2 at 110',exit_order('take_profit_market',110,.2,operation='tp-1'))
            tp2=record('round_2','TP fixed 0.3 at 120',exit_order('take_profit_market',120,.3,operation='tp-2'))
            tp_all=record('round_2','TP remaining all at 130',exit_order('take_profit_market',130,all_mode=True,operation='tp-all'))
            sl1=record('round_2','SL fixed 0.2 at 95',exit_order('stop_market',95,.2,operation='sl-1'))
            sl2=record('round_2','SL fixed 0.3 at 90',exit_order('stop_market',90,.3,operation='sl-2'))
            record('round_2','SL remaining all at 85',exit_order('stop_market',85,all_mode=True,operation='sl-all'))
            legacy=record('compatibility','legacy amount=0 ordinary limit snapshot',store.close(
                'LONG',0,'take_profit_limit',100,price=150,reason='Legacy fixed snapshot',operation_id='legacy'))

            store=MockIndependentTrading('offline-demo','ETH/USDT')
            restarted=store.snapshot()
            all_after_restart=store.describe_order(tp_all['order_id'])
            assert all_after_restart['amount'] is None and all_after_restart['remaining'] is None
            assert store.describe_order(legacy['order_id'])['amount']==1
            record('restart','fixed and dynamic semantics retained',restarted)

            tp2=record('round_3','edit TP second trigger to 122',store.edit(
                tp2['order_id'],trigger_price=122,current_price=100,reason='Adjust target',operation_id='edit-tp-2'))
            tp_all=record('round_3','edit dynamic all trigger to 132',store.edit(
                tp_all['order_id'],trigger_price=132,current_price=100,reason='Adjust final target',operation_id='edit-tp-all'))
            record('round_3','cancel first SL stage',store.cancel(sl1['order_id'],operation_id='cancel-sl-1'))

            record('maintenance','first TP fills 0.2',store.monitor(111,100,base_time+120000))
            record('maintenance','second TP fills 0.3',store.monitor(123,115,base_time+240000))
            before_final=store.snapshot()
            assert store.describe_order(sl2['order_id'])['remaining_amount']==.3
            assert store.describe_order(legacy['order_id'])['remaining_amount']==1
            assert before_final['quantity_conflict'] and not before_final['pending']
            record('round_4','fixed exits preserved and conflict observable',before_final)
            record('maintenance','final TP closes actual remaining 0.5',store.monitor(135,125,base_time+360000))
            final=store.snapshot()
            assert [(row['pos_side'],row['amount']) for row in final['positions']]==[('SHORT',.5)]
            assert final['exits']==[]
            assert abs(store.describe_order(tp_all['order_id'])['filled_amount']-.5)<1e-12
            record('round_5','LONG cycle cleared; SHORT remains',final)

            return dict(
                demonstration='Independent exits v2 — isolated simulation',
                network_used=False,production_configuration_changed=False,
                assumptions=['Simulation maintains separate LONG and SHORT inventory.',
                             'OHLC matching checks stop losses first, then nearest levels; it does not reconstruct intrabar chronology.',
                             'Conditional limit children start matching on a later bar; this demo uses conditional market exits.'],
                checks=dict(restart_all_remains_nullable=True,legacy_zero_remains_fixed=True,
                            fixed_orders_not_resized=True,quantity_conflict_does_not_block_management=True,
                            long_actual_exit_amount=1,remaining_short_amount=.5,
                            take_profit_fills=[store.describe_order(tp1['order_id'])['filled_amount'],
                                               store.describe_order(tp2['order_id'])['filled_amount'],
                                               store.describe_order(tp_all['order_id'])['filled_amount']]),
                events=events)
    finally:
        database.DB_NAME=previous_database


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    evidence=demonstrate()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(output=str(args.output.resolve()),checks=evidence['checks']),ensure_ascii=False))
