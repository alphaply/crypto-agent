"""Resolve discovery credentials without persisting or changing runtime configuration."""
from backend.app.schemas.provider_models import ProviderDiscoveryRequest
from backend.config_store import load_effective_runtime_snapshot
from backend.utils.provider_catalog import CatalogError, connection_identity, fetch_models


def resolve_discovery_connection(request: ProviderDiscoveryRequest) -> dict:
    connection = request.model_dump(exclude={"credential"})
    identity = connection_identity(connection)
    connection["api_base"] = identity[0]
    credential = request.credential
    if credential.mode == "draft":
        key = credential.api_key.get_secret_value()
    elif credential.mode == "saved":
        saved = next((item for item in load_effective_runtime_snapshot().get("llm_providers", [])
                      if item.get("provider_id") == credential.provider_id), None)
        if saved is None:
            raise CatalogError("provider_not_found", 404)
        if connection_identity(saved) != identity:
            raise CatalogError("connection_changed", 400)
        key = saved.get("api_key") or ""
    else:
        key = ""
    if not key.strip():
        raise CatalogError("missing_credentials", 400)
    return {**connection, "api_key": key}


async def discover_provider_models(request: ProviderDiscoveryRequest) -> dict:
    return await fetch_models(resolve_discovery_connection(request))
