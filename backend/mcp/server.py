"""Official MCP SDK Streamable HTTP and OAuth AS integration for the FastAPI host."""
from __future__ import annotations

from contextlib import asynccontextmanager
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
    return [dict(item) for item in TOOL_CATALOG]


def build_server(public_url):
    provider = AuthorizationProvider(public_url)
    origin = urlparse(public_url)
    server = FastMCP(
        'Crypto Agent', stateless_http=True, json_response=True, streamable_http_path='/mcp',
        instructions='Shared market research and scoped trading. Start with list_profiles and list_symbols to discover authorized canonical symbols. Prefer flat buy_spot/create_spot_exit tools, and read get_spot_inventory before a spot exit. Read get_trading_tools before using execute_trade. Every write needs a unique operation_id; reuse the same ID only for the identical request. Unknown outcomes must be inspected, never blindly retried. Submitted orders are not fills.',
        token_verifier=provider,
        auth=AuthSettings(issuer_url=AnyHttpUrl(public_url + '/oauth'),
                          resource_server_url=AnyHttpUrl(public_url + '/mcp'),
                          required_scopes=['read'], validate_token_resource=True),
        transport_security=TransportSecuritySettings(allowed_hosts=[origin.netloc], allowed_origins=[public_url]),
    )
    register_tools(server)
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
            yield
