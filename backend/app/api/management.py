"""Explicit administrator lifecycle actions; reading status never claims."""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.app.core.deps import get_current_user
from backend.utils import visible_management


router = APIRouter(prefix='/api/management', tags=['management'], dependencies=[Depends(get_current_user)])


class ReleaseRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmed: bool = False


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_config_id: str = Field(min_length=1, max_length=200)
    accepted: bool = False


def _require_owner(config_id: str):
    from backend.config import config
    if config.get_config_by_id(config_id) is None:
        raise HTTPException(404, 'Management task/profile was not found')


def _lifecycle(callback, *args, **kwargs):
    try:
        result = callback(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(409, {'code': getattr(exc, 'code', 'management_lifecycle_conflict'),
                                  'message': str(exc), **getattr(exc, 'details', {})}) from None
    except Exception:
        raise HTTPException(502, {'code': 'management_observation_unavailable',
                                  'message': 'Resource verification failed; ownership is retained.'}) from None
    if result.get('status') == 'blocked':
        raise HTTPException(409, result)
    return {'success': True, **result}


@router.get('/{config_id}')
def status(config_id: str, response: Response):
    _require_owner(config_id)
    response.headers['Cache-Control'] = 'no-store'
    return {'success': True, 'management': visible_management.management_status(config_id),
            'recovery_targets': visible_management.management_recovery_targets(config_id)}


@router.post('/{config_id}/scopes/{claim_id}/release')
def release(config_id: str, claim_id: str, payload: ReleaseRequest):
    _require_owner(config_id)
    return _lifecycle(visible_management.release_management_scope, config_id, claim_id, confirmed=payload.confirmed)


@router.post('/{config_id}/scopes/{claim_id}/transfer')
def transfer(config_id: str, claim_id: str, payload: TransferRequest):
    _require_owner(config_id)
    _require_owner(payload.target_config_id)
    return _lifecycle(visible_management.transfer_management_scope, config_id, claim_id,
                      payload.target_config_id, accepted=payload.accepted)


@router.get('/{config_id}/recovery/{recovery_id}')
def recovery(config_id: str, recovery_id: str, response: Response):
    _require_owner(config_id)
    response.headers['Cache-Control'] = 'no-store'
    if visible_management.lookup_management_recovery(config_id, recovery_id) is None:
        raise HTTPException(404, 'Accepted recovery identity was not found for this manager')
    return _lifecycle(visible_management.query_management_recovery, config_id, recovery_id)
