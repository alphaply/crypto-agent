from backend.config import config as global_config

from backend.app.services.dashboard_service import (
    build_dashboard_overview,
    get_daily_summaries_payload,
    get_orders_payload,
)
from backend.app.services.stats_service import (
    get_equity_compare_payload,
    get_kline_payload,
    get_position_stats_payload,
    get_token_stats_payload,
)


def _usage_summary(payload: dict) -> dict:
    daily = payload.get("daily", [])
    models = payload.get("models", [])
    agents = payload.get("agents", [])
    total_tokens = sum(int(item.get("total", 0) or 0) for item in daily)
    total_cost = round(sum(float(item.get("cost", 0) or 0) for item in models), 4)
    latest_day = daily[0] if daily else None
    return {
        "total_tokens_14d": total_tokens,
        "total_cost": total_cost,
        "tracked_models": len(models),
        "tracked_agents": len(agents),
        "latest_day": latest_day,
    }


def build_public_dashboard_payload(symbol: str | None = None) -> dict:
    overview = build_dashboard_overview(symbol=symbol)
    usage = get_token_stats_payload()
    compare_candidates = [
        {
            "config_id": item.get("config_id"),
            "display_name": item.get("display_name"),
            "mode": item.get("mode"),
            "model": item.get("model"),
            "enabled": item.get("enabled"),
            "timestamp": item.get("timestamp"),
        }
        for item in overview.get("agent_summaries", [])
    ]
    return {
        **overview,
        "compare_candidates": compare_candidates,
        "default_compare_ids": [item["config_id"] for item in compare_candidates if item.get("config_id")],
        "usage_summary": _usage_summary(usage),
    }


def build_public_compare_payload(symbol: str, config_ids: str = "") -> dict:
    return get_equity_compare_payload(symbol, config_ids)


def build_public_workspace_payload(config_id: str, timeframe: str = "1h") -> dict:
    cfg = global_config.get_config_by_id(config_id)
    if not cfg:
        raise FileNotFoundError(f"Config not found: {config_id}")

    symbol = cfg.get("symbol")
    overview = build_dashboard_overview(symbol=symbol)
    agent = next(
        (item for item in overview.get("agent_summaries", []) if item.get("config_id") == config_id),
        None,
    )
    if not agent:
        raise FileNotFoundError(f"Workspace not found: {config_id}")

    return {
        "timeframe": timeframe,
        "agent": agent,
        "position": get_position_stats_payload(config_id),
        "orders": get_orders_payload(config_id, page=1, per_page=20),
        "daily_summaries": get_daily_summaries_payload(config_id, days=7),
        "kline": get_kline_payload(config_id, timeframe),
    }


def build_public_usage_payload() -> dict:
    payload = get_token_stats_payload()
    payload["summary"] = _usage_summary(payload)
    return payload
