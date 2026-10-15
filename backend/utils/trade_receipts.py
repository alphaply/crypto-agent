"""Pure operation receipts: execution facts and cycle maintenance are independent.

No exchange, database or clock access belongs in the projection. Callers supply
observations, so the same evidence produces the same receipt after a restart.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from backend.utils.exit_orders import canonical_order_type, observed_order_status, order_reference
from backend.utils.trade_order_evidence import classify_order, UNRESOLVED

OperationStatus = Literal['failed', 'pending', 'unknown', 'submitted', 'completed', 'skipped', 'not_executed']
TERMINAL = {'closed', 'filled', 'canceled', 'cancelled', 'expired', 'rejected'}


class TradeReceipt(BaseModel):
    model_config = ConfigDict(extra='allow')
    receipt_version: Literal[1] = 1
    contract_version: Literal[2] = 2
    status: OperationStatus
    operation_id: str | None
    action: str
    execution: dict
    position: dict
    cleanup: dict
    continuation: dict
    recovery: dict


@dataclass
class TradeAttempt:
    operation_id: str | None
    action: str
    phase: str = 'preflight'
    submission_state: str = 'not_submitted'
    plan: dict | None = None
    record: dict | None = None
    admission: dict | None = None
    arguments: dict | None = None


current_attempt: ContextVar[TradeAttempt | None] = ContextVar('trade_attempt', default=None)


def bind_attempt(plan: dict, record: dict, phase: str) -> TradeAttempt | None:
    attempt = current_attempt.get()
    if attempt and attempt.operation_id in {
        record.get('operation_id'), record.get('cancel_operation_id'),
        record.get('last_operation_id'), (record.get('amendment') or {}).get('operation_id'),
    }:
        attempt.plan, attempt.record, attempt.phase = plan, record, phase
        return attempt
    return None


def unresolved_orders(plan: dict) -> list[dict]:
    def blocking_operation(record):
        for key in ('replacement', 'amendment'):
            pending = record.get(key) or {}
            if pending and (key == 'replacement' or pending.get('state') in {'pending', 'terminal_unconfirmed'}):
                return pending.get('operation_id') or record.get('operation_id')
        if record.get('cancel_requested_at') and record.get('status') not in TERMINAL:
            return record.get('cancel_operation_id') or record.get('operation_id')
        return record.get('operation_id')
    return [dict(order_id=order_reference(r), client_id=r.get('client_id'),
                 operation_id=blocking_operation(r), order_status=observed_order_status(r),
                 side=plan.get('side'), kind='exit' if r.get('exit_type') else 'entry',
                 reason_code='fill_unverified' if r.get('fill_pending') else 'order_unresolved',
                 last_error=r.get('query_error'), last_attempt_at=r.get('last_attempt_at'),
                 next_retry_at=r.get('next_retry_at'), attempt_count=r.get('attempt_count', 0))
            for r in plan.get('entries', []) + plan.get('exits', [])
            if classify_order(r)['state'] in UNRESOLVED]


def project_receipt(plan: dict | None, record: dict | None, *, operation_id: str | None,
                    action: str, base: dict | None = None, phase: str = 'verify',
                    submission_state: str | None = None, error: str | None = None) -> dict:
    plan, record = plan or {}, record or {}
    result = dict(base or {})
    factor = float(plan.get('contract_size') or 1)
    native = record.get('status')
    verified = bool(record.get('fill_verified') and not record.get('fill_pending'))
    filled = float(record.get('filled') or 0) * factor if verified else None
    amount = record.get('execution_amount', record.get('submission_amount')) if record.get('quantity_mode') == 'all' else record.get('amount')
    remaining = max(float(amount) * factor - filled, 0) if amount is not None and filled is not None else None
    amendment = record.get('amendment') or {}
    submission = submission_state or record.get('submission_state') or ('submitted' if record.get('id') else 'unknown')
    if native == 'rejected' or (action == 'edit' and (record.get('replacement_error') or amendment.get('state') == 'rejected')):
        status = 'failed'
    elif submission == 'not_submitted':
        status = 'failed'
    elif not record.get('id') or submission == 'unknown':
        status = 'unknown'
    elif record.get('fill_pending'):
        status = 'pending'
    elif action == 'cancel':
        status = 'completed' if native in TERMINAL and verified else 'pending'
    elif action == 'edit' and (record.get('replacement') or amendment.get('state') in {'pending', 'terminal_unconfirmed'}):
        status = 'pending'
    elif action == 'edit' and amendment.get('state') == 'confirmed':
        status = 'completed'
    elif action == 'edit' and record.get('replaces') and record.get('id'):
        status = 'completed'
    elif native in TERMINAL:
        status = 'completed' if verified else 'pending'
    else:
        status = 'submitted'
    if result.get('status') in {'skipped', 'not_executed'}:
        status = result['status']
    blockers = unresolved_orders(plan)
    full_close_unobserved = bool(action == 'close' and record.get('quantity_mode') == 'all'
                                and native in {'filled','closed'} and verified
                                and plan.get('verification_pending') and plan.get('state') != 'DONE')
    if full_close_unobserved:
        existing = {item.get('order_id') for item in blockers}
        for other in plan.get('entries',[]) + plan.get('exits',[]):
            if other is not record and other.get('status') not in TERMINAL and order_reference(other) not in existing:
                blockers.append({'order_id':order_reference(other),'client_id':other.get('client_id'),
                                 'operation_id':other.get('operation_id'),'reason_code':'cleanup_not_observed'})
    cleanup_pending = bool(plan.get('state') == 'EXITING' or plan.get('cleanup_fill_unseen')
                           or plan.get('unobserved_entry_fills')
                           or (plan.get('residual_verified') and (plan.get('position_observation') or {}).get('amount'))
                           or (blockers and status in {'completed', 'submitted'}) or full_close_unobserved)
    cleanup_status = 'pending' if cleanup_pending else 'completed' if plan.get('state') == 'DONE' else 'not_required'
    position = dict(plan.get('position_observation') or {
        'side': plan.get('side'), 'amount': None, 'status': 'unknown', 'observed_at': None})
    write_times = [float(r.get(key) or 0) for r in plan.get('entries', []) + plan.get('exits', [])
                   for key in ('created_at', 'cancel_requested_at', 'submitted_at', 'acknowledged_at')]
    write_times += [float((r.get('amendment') or {}).get('requested_at') or 0)
                    for r in plan.get('entries', []) + plan.get('exits', [])]
    if max(write_times, default=0) > float(position.get('observed_at') or 0):
        position['status'] = 'stale' if position.get('observed_at') else 'unknown'
    if plan.get('position_error'):
        position.update(status='stale' if position.get('observed_at') else 'unknown', error=plan['position_error'])
    if position['status'] != 'verified' and position.get('amount') is not None:
        position.update(last_known_amount=position['amount'], amount=None)
    from backend.utils.independent_admission import continuation_for
    continuation = continuation_for({**plan, 'position_observation': position})
    is_converged_terminal = bool(record.get('status') in TERMINAL and not record.get('fill_pending') and not cleanup_pending)
    result.update(receipt_version=1, contract_version=2, status=status, operation_id=operation_id,
                  action=action, episode_id=plan.get('episode_id'), pending=status in {'unknown', 'pending'},
                  filled_amount=filled, remaining_amount=remaining, actual_remaining=remaining,
                  last_known_filled_amount=float(record.get('filled') or 0) * factor if record.get('fill_verified') else None,
                  last_known_fill_at=record.get('fill_observed_at'),
                  execution={'phase': phase, 'submission_state': submission,
                             'fill_status': 'verified' if verified else 'unverified'},
                  position=position,
                  cleanup={'status': cleanup_status, 'orders': blockers,
                           'error': plan.get('error') if cleanup_pending else None},
                  continuation=continuation,
                  recovery={'state': 'background',
                            'target_operation_id': operation_id if record else next((r['operation_id'] for r in blockers if r.get('operation_id')), operation_id),
                            'blocker_operation_ids': list(dict.fromkeys(r['operation_id'] for r in blockers if r.get('operation_id'))),
                            'last_error': record.get('query_error'),
                            'last_attempt_at': record.get('last_attempt_at'),
                            'next_retry_at': None if is_converged_terminal else record.get('next_retry_at'),
                            'attempt_count': record.get('attempt_count', 0)})
    if record:
        result.update(id=record.get('id'), client_id=record.get('client_id'), order_id=order_reference(record),
                      order_status=observed_order_status(record), order_type=canonical_order_type(record),
                      quantity_mode=record.get('quantity_mode', 'fixed'), side=plan.get('side'),
                      exchange_order_id=record.get('execution_order_id') or record.get('id'),
                      exchange_channel='regular' if record.get('execution_order_id') else record.get('exchange_channel'),
                      parent_order_id=order_reference(record, current=False) if record.get('execution_order_id') else None,
                      requested_amount=record.get('requested_amount'), resolved_amount=record.get('resolved_amount'))
    result.pop('error', None)
    attempt = current_attempt.get()
    result['admission'] = (attempt.admission if attempt and attempt.admission else
                           {'version': 1, 'decision': 'not_evaluated', 'allowed': None})
    if status in {'failed', 'unknown', 'pending'}:
        result['error'] = record.get('replacement_error') or record.get('execution_error') or error
    return TradeReceipt.model_validate(result).model_dump()
