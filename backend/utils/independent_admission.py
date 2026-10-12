"""Dependencies for independent writes, evaluated using fresh account evidence."""
from backend.utils.trade_receipts import unresolved_orders


def assess_admission(plan: dict, action: str, *, hedged: bool, opposite: dict | None = None) -> dict:
    blockers = unresolved_orders(plan)
    reason = None
    if action != 'cancel':
        if blockers or plan.get('read_errors') or plan.get('position_error'):
            reason = 'order_evidence_unverified'
        elif plan.get('state') == 'EXITING' or plan.get('cleanup_fill_unseen') or plan.get('unobserved_entry_fills'):
            reason = 'cycle_cleanup_unverified'
        elif plan.get('error'):
            reason = 'position_evidence_unverified'
        if not hedged and opposite and opposite.get('state') != 'DONE':
            opposite_blockers = unresolved_orders(opposite)
            if (opposite_blockers or opposite.get('error') or opposite.get('read_errors') or opposite.get('position_error')
                    or opposite.get('state') == 'EXITING' or opposite.get('cleanup_fill_unseen') or opposite.get('unobserved_entry_fills')):
                blockers.extend(opposite_blockers)
                reason = 'one_way_shared_position_unverified'
    return {'allowed': reason is None, 'reason_code': reason, 'side': plan.get('side'),
            'blocked_action': action if reason else None, 'blockers': blockers,
            'assessed_at': plan.get('verified_at')}
