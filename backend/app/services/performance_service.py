"""Local read-only metrics and explicit ledger detail retrieval."""
from backend.config import config
from backend.utils.performance_context import performance_data


def get_performance_payload(config_id: str, hours: int = 168, *, details: bool = False) -> dict:
    cfg = config.get_config_by_id(config_id)
    if not cfg:
        raise FileNotFoundError('Config not found')
    data = performance_data(config_id, cfg.get('symbol', ''), cfg.get('mode', 'STRATEGY'),
                            hours=hours, include_details=details)
    evidence = data.pop('details', None)
    return {'performance': data, **({'details': evidence} if details else {})}
