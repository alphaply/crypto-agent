"""Separate independent order observations from authorized cleanup intent."""
from copy import deepcopy

from backend.utils.trade_order_evidence import classify_order


def cleanup_authorized(plan: dict) -> bool:
    """Only a completed Agent exit or already-started cleanup authorizes writes.

    A flat exchange snapshot, including one following manual trading, is an
    observation. It cannot manufacture a request to cancel other orders.
    """
    if (plan.get('cleanup_intent') or {}).get('source') == 'agent_exit':
        return True
    for record in plan.get('exits', []):
        # Legacy v2 orders retain an explicit "all" instruction. Neither an
        # external baseline nor accumulated partial fills establishes intent.
        full_close = (record.get('cleanup_on_fill') is True or
                      ('cleanup_on_fill' not in record and record.get('quantity_mode') == 'all'))
        if (full_close and record.get('source', 'agent') == 'agent'
                and record.get('status') in {'closed', 'filled'}
                and record.get('fill_verified') and not record.get('fill_pending')
                and float(record.get('filled') or 0) > 0):
            return True
    return False


def observe_flat(plan: dict, observed_at: float) -> None:
    """Keep live orders manageable without scheduling their cancellation."""
    outstanding = any(classify_order(r)['state'] not in {'terminal_verified', 'not_accepted'}
                      for r in plan.get('entries', []) + plan.get('exits', []))
    plan.update(state='WAITING' if outstanding else 'DONE', error=None)
    plan.setdefault('external_flat_observation', {'observed_at': observed_at})
    plan.pop('flat_candidate', None)


def retain_live_orders(previous: dict, successor: dict) -> None:
    """Carry management references across an observed boundary, not executions."""
    for kind in ('entries', 'exits'):
        for record in previous.get(kind, []):
            if classify_order(record)['state'] in {'terminal_verified', 'not_accepted'}:
                continue
            retained = deepcopy(record)
            retained.setdefault('origin_episode_id', previous['episode_id'])
            successor[kind].append(retained)
