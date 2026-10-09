"""Official MCP SDK Streamable HTTP and OAuth AS integration for the FastAPI host."""
from __future__ import annotations

from contextlib import asynccontextmanager
from functools import partial
from typing import Literal
from urllib.parse import urlparse

import anyio
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes, cors_middleware
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.routing import Route

from .auth import AuthorizationProvider, sdk_revoke
from . import service
from .settings import settings, SCOPES

_server = None
_public_url = None


def active_public_url():
    return _public_url


def tool_catalog():
    return [
        {'name': 'list_profiles', 'scope': 'read', 'description': 'List authorized MCP connection profiles'},
        {'name': 'get_news', 'scope': 'read', 'description': 'Read the shared scored news summary'},
        {'name': 'get_market', 'scope': 'read', 'description': 'Read current ticker and OHLCV candles'},
        {'name': 'get_balance', 'scope': 'read', 'description': 'Read exchange balances'},
        {'name': 'get_positions', 'scope': 'read', 'description': 'Read perpetual positions'},
        {'name': 'get_orders', 'scope': 'read', 'description': 'Read open exchange orders'},
        {'name': 'get_trading_tools', 'scope': 'read', 'description': 'Read permitted trade tool schemas'},
        {'name': 'execute_trade', 'scope': 'trade / cancel', 'description': 'Execute existing spot/perpetual tools with a persistent operation ID'},
        {'name': 'get_operation', 'scope': 'read', 'description': 'Read the original operation receipt without replaying it'},
    ]


def build_server(public_url):
    provider = AuthorizationProvider(public_url)
    origin = urlparse(public_url)
    server = FastMCP(
        'Crypto Agent', stateless_http=True, json_response=True, streamable_http_path='/mcp',
        instructions='Shared market research and scoped trading. Read get_trading_tools before executing. Every write needs a unique operation_id; reuse the same ID only for the identical request. Unknown outcomes must be inspected, never blindly retried. Submitted orders are not fills.',
        token_verifier=provider,
        auth=AuthSettings(issuer_url=AnyHttpUrl(public_url + '/oauth'),
                          resource_server_url=AnyHttpUrl(public_url + '/mcp'),
                          required_scopes=['read'], validate_token_resource=True),
        transport_security=TransportSecuritySettings(allowed_hosts=[origin.netloc], allowed_origins=[public_url]),
    )
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
    read_meta = {'securitySchemes': [{'type': 'oauth2', 'scopes': ['read']}]}

    async def query(resource, **kwargs):
        return await anyio.to_thread.run_sync(partial(service.query, service.principal(), resource, **kwargs))

    @server.tool(annotations=read, meta=read_meta)
    async def list_profiles() -> list[dict]:
        """List connection profiles authorized for this caller. No exchange secrets are exposed."""
        return await query('profiles')

    @server.tool(annotations=read, meta=read_meta)
    async def get_news() -> dict:
        """Read the latest shared, scored news digest with source timestamps and freshness."""
        return await query('news')

    @server.tool(annotations=read, meta=read_meta)
    async def get_market(profile_id: str, symbol: str, timeframe: Literal['15m', '1h', '4h', '1d', '1w'] = '1h', limit: int = 120) -> dict:
        """Read ticker and candles for a symbol allowed by this connection profile."""
        return await query('market', profile_id=profile_id, symbol=symbol, timeframe=timeframe, limit=limit)

    @server.tool(annotations=read, meta=read_meta)
    async def get_balance(profile_id: str) -> dict:
        """Read total, available and used exchange balances; shared accounts must not be summed per agent."""
        return await query('balance', profile_id=profile_id)

    @server.tool(annotations=read, meta=read_meta)
    async def get_positions(profile_id: str, symbol: str | None = None) -> list[dict]:
        """Read open perpetual positions for authorized symbols; omit symbol for all allowed positions. Use balance for spot holdings."""
        return await query('positions', profile_id=profile_id, symbol=symbol)

    @server.tool(annotations=read, meta=read_meta)
    async def get_orders(profile_id: str, symbol: str, limit: int = 100) -> list[dict]:
        """Read open exchange orders. Trading tools only amend/cancel orders owned by this MCP profile."""
        return await query('orders', profile_id=profile_id, symbol=symbol, limit=limit)

    @server.tool(annotations=read, meta=read_meta)
    async def get_trading_tools(profile_id: str) -> list[dict]:
        """Get complete argument schemas for existing spot buy/cancel and perpetual open/close/amend/protection/batch tools."""
        return await query('tools', profile_id=profile_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True), meta={'securitySchemes': [{'type': 'oauth2', 'scopes': SCOPES}]})
    async def execute_trade(profile_id: str, symbol: str, tool_name: str, arguments: dict, operation_id: str) -> dict:
        """Execute a tool listed by get_trading_tools. Requires trade scope (cancel scope for cancellations). operation_id must be stable across retries; a timeout remains unknown and is never blindly replayed. Spot orders in one call share one allowance in their common quote currency; mixed quote currencies in a call are rejected. MCP leverage limits use actual exchange settings; reducing/closing/cancelling remains possible above the cap."""
        return await anyio.to_thread.run_sync(partial(service.execute, service.principal(), profile_id, symbol, tool_name, arguments, operation_id))

    @server.tool(annotations=read, meta=read_meta)
    async def get_operation(profile_id: str, operation_id: str) -> dict:
        """Read this caller's persisted operation result without submitting trades again."""
        return await anyio.to_thread.run_sync(partial(service.operation, service.principal(), profile_id, operation_id))

    return server, provider


def install_mcp(app):
    """Call before SPA catchall routes. The host must enter mcp_lifespan once."""
    global _server, _public_url
    from .management import router
    _public_url = settings()['public_url']
    _server, provider = build_server(_public_url)
    app.include_router(router)
    http_app = _server.streamable_http_app()
    # Exact ASGI delegation keeps /mcp without a redirect or a catchall mount.
    app.router.routes.append(Route('/mcp', endpoint=http_app))
    app.router.routes.extend(create_protected_resource_routes(
        AnyHttpUrl(_public_url + '/mcp'), [AnyHttpUrl(_public_url + '/oauth')], scopes_supported=SCOPES))
    auth_routes = create_auth_routes(provider, AnyHttpUrl(_public_url + '/oauth'),
                                    client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=SCOPES, default_scopes=SCOPES),
                                    revocation_options=RevocationOptions(enabled=True))
    auth_routes = [route for route in auth_routes if route.path != '/revoke']
    auth_routes.append(Route('/revoke', endpoint=cors_middleware(partial(sdk_revoke, provider), ['POST', 'OPTIONS']), methods=['POST', 'OPTIONS']))
    auth_routes.append(Route('/consent', endpoint=provider.consent, methods=['GET', 'POST']))
    # RFC 8414 places the issuer path after the well-known prefix.
    app.router.routes.append(Route('/.well-known/oauth-authorization-server/oauth', endpoint=auth_routes[0].app, methods=['GET', 'OPTIONS']))
    app.mount('/oauth', Starlette(routes=auth_routes))
    return _server


@asynccontextmanager
async def mcp_lifespan():
    if _server is None:
        yield
    else:
        async with _server.session_manager.run():
            yield
