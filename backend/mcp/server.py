"""Official MCP SDK Streamable HTTP and OAuth AS integration for the FastAPI host."""
from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
from functools import partial
from urllib.parse import urlparse

from mcp.server.auth.routes import build_metadata, create_auth_routes, create_protected_resource_routes, cors_middleware
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse

from .auth import AuthorizationProvider, sdk_revoke
from .settings import settings, SCOPES
from .discovery import DiscoveryHeadersMiddleware
from .tools import TOOL_CATALOG, register_tools

_server = None
_public_url = None


def active_public_url():
    return _public_url


def tool_catalog():
    from .paper_tools import PAPER_TOOL_CATALOG
    from .paper_advanced_tools import PAPER_ADVANCED_CATALOG
    from .canonical_tools import CORE_NAMES, PAPER_NAMES
    legacy = {item['name']: item for item in [*TOOL_CATALOG, *PAPER_TOOL_CATALOG, *PAPER_ADVANCED_CATALOG]}
    return [dict(legacy.get(name, {'name': name, 'scope': 'trade' if name == 'configure_paper_account' else 'read',
                                  'description': 'Canonical scoped trading capability'}))
            for name in (*CORE_NAMES, *PAPER_NAMES)]


def build_server(public_url):
    provider = AuthorizationProvider(public_url)
    origin = urlparse(public_url)
    server = FastMCP(
        'Crypto Agent', stateless_http=True, json_response=True, streamable_http_path='/mcp',
        instructions='Start with get_trading_context for authorized profiles or paper rules and your bot identity. Search unfamiliar symbols with search_symbols; preserve returned symbol and exchange. Read only required get_account_state facets or get_trade_records. Use execute_trade_actions contract_version=3 for one or up to 20 typed ordered actions; execution follows current mode capabilities, permissions and management ownership. Visible manual resources can be managed within authorized scope without a preliminary adopt call; queries never claim. Submitted orders are not fills. Reuse operation_id only for identical intent, inspect uncertainty with get_operation, and never resume a skipped tail by replay. Paper uses real public production prices and simulates only balances/orders/execution; trusted user_id is the sender or your returned bot identity. Paper bank, account settings and ranking have separate tools; notification polling belongs to the bot HTTP integration. Legacy cached names remain authorized compatibility calls.',
        token_verifier=provider,
        auth=AuthSettings(issuer_url=AnyHttpUrl(public_url + '/oauth'),
                          resource_server_url=AnyHttpUrl(public_url + '/mcp'),
                          required_scopes=['read'], validate_token_resource=True),
        transport_security=TransportSecuritySettings(allowed_hosts=[origin.netloc], allowed_origins=[public_url]),
    )
    register_tools(server)
    from .paper_tools import register_paper_tools
    register_paper_tools(server)
    return server, provider


def install_mcp(app):
    """Call before SPA catchall routes. The host must enter mcp_lifespan once."""
    global _server, _public_url
    # Route construction reads persisted OAuth settings before the ASGI
    # lifespan runs. This is startup migration, never a per-request side effect.
    from backend import database
    from . import store
    with database.get_db_conn() as conn:
        store.initialize_schema(conn)
        conn.commit()
    from .management import router
    from .paper_competition import bootstrap
    bootstrap()
    _public_url = settings()['public_url']
    _server, provider = build_server(_public_url)
    app.include_router(router)
    from .paper_api import router as paper_router
    app.include_router(paper_router)
    http_app = _server.streamable_http_app()
    # Exact ASGI delegation keeps /mcp without a redirect or a catchall mount.
    app.router.routes.append(Route('/mcp', endpoint=http_app))
    resource_routes = create_protected_resource_routes(
        AnyHttpUrl(_public_url + '/mcp'), [AnyHttpUrl(_public_url + '/oauth')], scopes_supported=SCOPES)
    app.router.routes.extend(resource_routes)
    app.router.routes.append(Route('/.well-known/oauth-protected-resource', endpoint=resource_routes[0].app, methods=['GET', 'OPTIONS']))
    registration = ClientRegistrationOptions(enabled=True, valid_scopes=SCOPES, default_scopes=SCOPES)
    revocation = RevocationOptions(enabled=True)
    auth_routes = create_auth_routes(provider, AnyHttpUrl(_public_url + '/oauth'),
                                    client_registration_options=registration,
                                    revocation_options=revocation)
    metadata = build_metadata(AnyHttpUrl(_public_url + '/oauth'), None, registration, revocation).model_dump(mode='json', exclude_none=True)
    # Public clients are accepted by SDK ClientAuthenticator and covered by our
    # PKCE tests; advertise that capability alongside confidential clients.
    metadata['token_endpoint_auth_methods_supported'] = ['none', 'client_secret_post', 'client_secret_basic']

    async def oauth_metadata(request):
        return JSONResponse(metadata, headers={'Cache-Control': 'no-store'})

    metadata_app = cors_middleware(oauth_metadata, ['GET', 'OPTIONS'])
    auth_routes[0] = Route('/.well-known/oauth-authorization-server', endpoint=metadata_app, methods=['GET', 'OPTIONS'])
    auth_routes = [route for route in auth_routes if route.path != '/revoke']
    auth_routes.append(Route('/revoke', endpoint=cors_middleware(partial(sdk_revoke, provider), ['POST', 'OPTIONS']), methods=['POST', 'OPTIONS']))
    auth_routes.append(Route('/consent', endpoint=provider.consent, methods=['GET', 'POST']))
    auth_routes.append(Route('/resource-metadata', endpoint=resource_routes[0].app, methods=['GET', 'OPTIONS']))
    # RFC 8414 places the issuer path after the well-known prefix.
    app.router.routes.append(Route('/.well-known/oauth-authorization-server/oauth', endpoint=metadata_app, methods=['GET', 'OPTIONS']))
    app.router.routes.append(Route('/.well-known/oauth-authorization-server', endpoint=metadata_app, methods=['GET', 'OPTIONS']))
    app.mount('/oauth', Starlette(routes=auth_routes))
    app.add_middleware(DiscoveryHeadersMiddleware, public_url=_public_url)
    return _server


@asynccontextmanager
async def mcp_lifespan():
    if _server is None:
        yield
    else:
        async with _server.session_manager.run():
            from .paper import maintenance
            stop = asyncio.Event()
            worker = asyncio.create_task(maintenance(stop), name='paper-maintenance')
            try:
                yield
            finally:
                stop.set()
                await worker
