from fastapi import APIRouter, Depends, HTTPException

from backend.app.core.deps import get_current_user
from backend.app.schemas.trading_rules import CreateTradingRuleRequest, UpdateTradingRuleRequest
from backend.app.services import trading_rules_service as service
from backend.database_rules import RuleConflictError, RuleLockedError

router = APIRouter(prefix='/api/history/trading-rules', tags=['history'])


def _call(operation):
    try:
        return operation()
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuleConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuleLockedError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('')
def list_rules(config_id: str, _: dict = Depends(get_current_user)):
    return {'success': True, 'rules': _call(lambda: service.list_rules(config_id))}


@router.post('')
def create_rule(payload: CreateTradingRuleRequest, _: dict = Depends(get_current_user)):
    change = payload.model_dump(exclude={'config_id'})
    rules = _call(lambda: service.apply_rule_changes(payload.config_id, [{'action': 'add', **change}], actor='human'))
    return {'success': True, 'rule': rules[0]}


@router.patch('/{rule_id}')
def update_rule(rule_id: str, payload: UpdateTradingRuleRequest, _: dict = Depends(get_current_user)):
    change = payload.model_dump(exclude={'config_id'}, exclude_none=True)
    rules = _call(lambda: service.apply_rule_changes(payload.config_id, [{'action': 'update', 'rule_id': rule_id, **change}], actor='human'))
    return {'success': True, 'rule': rules[0]}


@router.get('/{rule_id}/history')
def rule_history(rule_id: str, config_id: str, _: dict = Depends(get_current_user)):
    return {'success': True, 'history': _call(lambda: service.rule_history(config_id, rule_id))}
