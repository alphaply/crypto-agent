from backend import database_rules
from backend.config import config


def require_task(config_id: str):
    if not config.get_config_by_id(config_id):
        raise FileNotFoundError('Config not found')


def list_rules(config_id: str):
    require_task(config_id)
    return database_rules.list_trading_rules(config_id)


def rule_history(config_id: str, rule_id: str):
    require_task(config_id)
    return database_rules.trading_rule_history(config_id, rule_id)


def apply_rule_changes(config_id: str, changes: list[dict], *, actor: str, operation_id: str | None = None):
    require_task(config_id)
    return database_rules.change_trading_rules(config_id, changes, actor=actor, operation_id=operation_id)
