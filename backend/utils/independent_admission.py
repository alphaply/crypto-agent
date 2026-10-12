"""Pure, target-scoped admission. Diagnostics never substitute for dependencies."""
from dataclasses import dataclass

from backend.utils.exit_orders import order_reference
from backend.utils.trade_order_evidence import classify_order


@dataclass(frozen=True)
class ActionIntent:
    action: str
    side: str | None = None
    target: str | None = None
    effect: str | None = None
    quantity_mode: str | None = None
    account_scope: str | None = None
    symbol: str | None = None
    changed_fields: tuple[str, ...] = ()


def action_intent(plan: dict, action: str, target: dict | None = None, arguments: dict | None = None) -> ActionIntent:
    args = arguments or {}
    effect = ('cancel_target' if action == 'cancel' else
              'increase_exposure' if action == 'open' else
              'modify_entry' if action == 'edit' and target and not target.get('exit_type') else
              'modify_exit' if action == 'edit' else
              'conditional_protection' if args.get('order_type') not in {None, 'market', 'limit'} else 'reduce_position')
    return ActionIntent(action, plan.get('side'), order_reference(target) if target else None, effect,
                        (target or {}).get('quantity_mode') or ('all' if args.get('close_all') else 'fixed'),
                        plan.get('account_scope'), plan.get('symbol'),
                        tuple(sorted(k for k in ('price', 'trigger_price', 'remaining_amount') if args.get(k) is not None)))


@dataclass(frozen=True)
class EvidenceSnapshot:
    plan: dict
    hedged: bool
    opposite: dict | None = None


def cycle_quiescent(plan: dict) -> bool:
    position = plan.get('position_observation') or {}
    return (position.get('status') == 'verified' and position.get('amount') == 0
            and not plan.get('cleanup_fill_unseen') and not plan.get('unobserved_entry_fills')
            and all(classify_order(r)['state'] in {'not_accepted', 'terminal_verified'}
                    for r in plan.get('entries', []) + plan.get('exits', [])))


def assess_admission(plan: dict, action: str | ActionIntent, *, hedged: bool,
                     opposite: dict | None = None, target: dict | None = None) -> dict:
    from backend.utils.trade_receipts import unresolved_orders
    intent = action if isinstance(action, ActionIntent) else action_intent(plan, action, target)
    snapshot = EvidenceSnapshot(plan, hedged, opposite)
    blockers = unresolved_orders(plan)
    warnings = []
    if plan.get('error'):
        warnings.append({'reason_code': 'historical_diagnostic', 'message': plan['error']})
    for record in plan.get('entries', []) + plan.get('exits', []):
        if classify_order(record)['state'] == 'not_accepted':
            warnings.append({'reason_code': 'previous_order_rejected', 'operation_id': record.get('operation_id')})
    if intent.action == 'cancel':
        blockers = [b for b in blockers if target and b.get('client_id') == target.get('client_id')]
        if target and target.get('id') and not target.get('replacement'):
            blockers = []  # _cancel resolves its current parent/child before sending.
    reason = 'order_evidence_unverified' if blockers else None
    position = plan.get('position_observation') or {}
    if intent.action != 'cancel':
        if plan.get('ownership_error'):
            reason = reason or 'ownership_evidence_unverified'
        if plan.get('position_error') or position.get('status') != 'verified':
            reason = reason or 'position_evidence_unverified'
        if (plan.get('cleanup_fill_unseen') or plan.get('unobserved_entry_fills')
                or (plan.get('state') == 'EXITING' and not cycle_quiescent(plan)
                    and intent.effect in {'increase_exposure', 'modify_entry'})):
            reason = reason or 'cycle_cleanup_unverified'
        if not snapshot.hedged and opposite and opposite.get('state') != 'DONE':
            shared = unresolved_orders(opposite)
            if (shared or opposite.get('position_error') or opposite.get('cleanup_fill_unseen')
                    or opposite.get('unobserved_entry_fills') or opposite.get('state') == 'EXITING'):
                blockers.extend(shared)
                reason = 'one_way_shared_position_unverified'
    if reason and not blockers:
        blockers = [{'reason_code': reason, 'side': plan.get('side'),
                     'missing_evidence': 'current_ownership' if plan.get('ownership_error') else 'current_position_or_cycle',
                     'last_error': plan.get('ownership_error') or plan.get('position_error'), 'order_id': None}]
    for blocker in blockers:
        blocker['recovery'] = {'tool': 'get_trade_operation', 'target_operation_id': blocker.get('operation_id')}
        blocker['affected_effect'] = intent.effect
        blocker['missing_evidence'] = blocker.get('missing_evidence') or 'verified_execution_outcome'
    not_evaluated = (intent.action in {'edit', 'cancel'} and target is None)
    if not_evaluated:
        reason = 'target_not_assessed'
    return {'version': 1, 'decision': 'not_evaluated' if not_evaluated else 'deny' if reason else 'allow_with_warning' if warnings else 'allow',
            'allowed': reason is None, 'reason_code': reason, 'side': plan.get('side'),
            'intent': vars(intent), 'scope': {k: plan.get(k) for k in ('account_scope', 'symbol', 'side', 'episode_id')},
            'blocked_action': intent.action if reason else None, 'blockers': blockers, 'warnings': warnings,
            'evidence_versions': {'cycle': plan.get('record_version'), 'scope': plan.get('scope_generation')},
            'assessed_at': plan.get('verified_at'),
            'refresh': {'performed': bool(plan.get('verified_at')), 'read_errors': plan.get('read_errors', [])}}


def continuation_for(plan: dict, *, hedged: bool | None = None, opposite: dict | None = None) -> dict:
    hedged = plan.get('hedged', True) if hedged is None else hedged
    opposite = opposite if opposite is not None else plan.get('opposite_evidence')
    decisions = [assess_admission(plan, a, hedged=hedged, opposite=opposite) for a in ('open', 'close')]
    rules = [{'action': d['intent']['action'], 'scope': d['scope'], 'reason_code': d['reason_code'],
              'blockers': d['blockers']} for d in decisions if not d['allowed']]
    for record in plan.get('entries', []) + plan.get('exits', []):
        if not record.get('id'):
            continue
        for action in ('edit', 'cancel'):
            decision = assess_admission(plan, action, hedged=hedged, opposite=opposite, target=record)
            if not decision['allowed']:
                rules.append({'action': action, 'target': order_reference(record), 'scope': decision['scope'],
                              'reason_code': decision['reason_code'], 'blockers': decision['blockers']})
    verified = bool(plan.get('verified_at') and not plan.get('position_error') and not plan.get('ownership_error'))
    blockers = [b for rule in rules for b in rule['blockers']]
    return {'blocked_actions': [d['intent']['action'] for d in decisions if not d['allowed']],
            'rules': rules, 'assessment_state': 'verified' if verified else 'needs_refresh',
            'reason_code': next((d['reason_code'] for d in decisions if not d['allowed']), None),
            'side': plan.get('side'), 'blocker_order_ids': list(dict.fromkeys(b.get('order_id') for b in blockers if b.get('order_id'))),
            'assessed_at': plan.get('verified_at'), 'advisory': True, 'requires_action_assessment': True,
            'scope': decisions[0]['scope'], 'evidence_versions': decisions[0]['evidence_versions']}
