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
from backend.utils.deployment_mode import maintenance_enabled

_server = None
_public_url = None


def active_public_url():
    return _public_url


_CANONICAL_CATALOG_META = {
    'get_trading_context': {
        'scope': 'read',
        'category': 'shared',
        'title_zh': '获取交易上下文与规则',
        'title_en': 'Get trading context & rules',
        'description_zh': '查询已授权的账户配置、模拟交易规则及机器人自身身份 ID。',
        'description_en': 'Read authorized profiles, paper trading rules, and bot identity.',
    },
    'search_symbols': {
        'scope': 'read',
        'category': 'shared',
        'title_zh': '搜索可交易标的',
        'title_en': 'Search tradable symbols',
        'description_zh': '按关键词搜索真实交易所上可交易的现货或永续合约标的。',
        'description_en': 'Search active spot or linear perpetual symbols across allowed markets.',
    },
    'get_market': {
        'scope': 'read',
        'category': 'shared',
        'title_zh': '查询实时行情与指标',
        'title_en': 'Get market quote & indicators',
        'description_zh': '获取真实盘口报价、K 线、技术指标及衍生品情绪；可按需只查 ticker 提速。',
        'description_en': 'Read real-time ticker, candles, technical indicators, and sentiment.',
    },
    'get_news': {
        'scope': 'read',
        'category': 'shared',
        'title_zh': '读取市场共享情报',
        'title_en': 'Read shared news digest',
        'description_zh': '读取系统最新聚合整理的市场新闻与情绪评分摘要。',
        'description_en': 'Read the latest aggregated market news and sentiment summary.',
    },
    'get_account_state': {
        'scope': 'read',
        'category': 'trading',
        'title_zh': '查询账户资产与持仓',
        'title_en': 'Get account balances & positions',
        'description_zh': '按需读取余额、持仓、现货库存、挂单或模拟交易周期盈亏统计。',
        'description_en': 'Read requested balances, positions, inventory, open orders, or paper stats.',
    },
    'get_trade_records': {
        'scope': 'read',
        'category': 'trading',
        'title_zh': '查询委托与成交历史',
        'title_en': 'Get orders & trade history',
        'description_zh': '分页查询当前挂单、单笔委托详情、成交明细或账本历史。',
        'description_en': 'Inspect open/ended orders, fill history, or ledger activity records.',
    },
    'execute_trade_actions': {
        'scope': 'trade / cancel',
        'category': 'trading',
        'title_zh': '执行交易指令',
        'title_en': 'Execute trade actions',
        'description_zh': '统一执行开仓、平仓、止盈止损、改单及撤单操作，支持多步有序动作。',
        'description_en': 'Execute ordered open, close, protection, edit, or cancel actions.',
    },
    'paper_bank': {
        'scope': 'trade',
        'category': 'paper',
        'title_zh': '模拟银行借还款',
        'title_en': 'Paper bank borrow / repay',
        'description_zh': '为模拟账户借入 USDT 或偿还借款（每日北京时间 00:00 计息）。',
        'description_en': 'Borrow USDT or repay debt in a paper account with daily interest.',
    },
    'configure_paper_account': {
        'scope': 'trade',
        'category': 'paper',
        'title_zh': '管理模拟账户与设置',
        'title_en': 'Configure paper account',
        'description_zh': '开户、修改昵称/默认杠杆/提醒偏好，或按用户明确请求重置破产账户。',
        'description_en': 'Create an account, update preferences, or reset a bankrupt account.',
    },
    'get_paper_leaderboard': {
        'scope': 'read',
        'category': 'paper',
        'title_zh': '查看模拟交易排行榜',
        'title_en': 'Get paper leaderboard',
        'description_zh': '查看收益率榜或下海（破产重置）榜，并可对比用户与机器人排名。',
        'description_en': 'View return or reset rankings and compare user vs. bot ranks.',
    },
    'paper_transfer': {
        'scope': 'trade',
        'category': 'paper',
        'title_zh': '模拟账户 USDT 转账',
        'title_en': 'Paper USDT transfer',
        'description_zh': '同一 QQBot 命名空间内向其他用户或机器人转出可用 USDT。',
        'description_en': 'Transfer available USDT to another account within the same namespace.',
    },
    'adjust_paper_position': {
        'scope': 'trade',
        'category': 'paper',
        'title_zh': '调整模拟持仓杠杆或保证金',
        'title_en': 'Adjust paper position',
        'description_zh': '在线调整已有逐仓模拟持仓的杠杆倍数，或追加/减少保证金。',
        'description_en': 'Change leverage or add/reduce margin on an existing isolated paper position.',
    },
}


def tool_catalog():
    from .paper_tools import PAPER_TOOL_CATALOG
    from .paper_advanced_tools import PAPER_ADVANCED_CATALOG
    from .canonical_tools import CORE_NAMES, PAPER_NAMES
    legacy = {item['name']: item for item in [*TOOL_CATALOG, *PAPER_TOOL_CATALOG, *PAPER_ADVANCED_CATALOG]}
    items = []
    for name in (*CORE_NAMES, *PAPER_NAMES):
        meta = _CANONICAL_CATALOG_META.get(name, {})
        base = legacy.get(name, {})
        items.append({
            'name': name,
            'scope': meta.get('scope') or base.get('scope', 'read'),
            'category': meta.get('category', 'paper' if name in PAPER_NAMES else 'trading'),
            'title_zh': meta.get('title_zh', name),
            'title_en': meta.get('title_en', name),
            'description_zh': meta.get('description_zh') or base.get('description', ''),
            'description_en': meta.get('description_en') or base.get('description', ''),
            'description': meta.get('description_en') or base.get('description', 'Canonical scoped trading capability'),
        })
    return items


def build_server(public_url):
    provider = AuthorizationProvider(public_url)
    origin = urlparse(public_url)
    server = FastMCP(
        'Crypto Agent', stateless_http=True, json_response=True, streamable_http_path='/mcp',
        instructions='Start with get_trading_context for authorized profiles or paper rules and your bot identity. Search unfamiliar symbols with search_symbols; preserve returned symbol and exchange. Read only required get_account_state facets or get_trade_records. Use execute_trade_actions contract_version=3 for one or up to 20 typed ordered actions; execution follows current mode capabilities, permissions and management ownership. Visible manual resources can be managed within authorized scope without a preliminary adopt call; queries never claim. Submitted orders are not fills. Reuse operation_id only for identical intent. Financial calls return factual acknowledgement promptly; the system verifies original identities in the background without a second operation-query tool or resuming a skipped tail. Paper uses real public production prices and simulates only balances/orders/execution; trusted user_id is the sender or your returned bot identity. Paper bank, account settings, transfers, existing isolated-position leverage/margin adjustments and ranking have separate tools; notification polling belongs to the bot HTTP integration. Legacy cached names remain authorized compatibility calls.',
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
    # Called after the host has checked the deployed schema and loaded config.
    from backend import database
    from . import store
    with database.get_db_conn() as conn:
        store.initialize_schema(conn)
        conn.commit()
    from .management import router
    from .paper_competition import bootstrap
    if not maintenance_enabled():
        bootstrap()
    _public_url = settings()['public_url']
    _server, provider = build_server(_public_url)
    app.include_router(router)
    from .paper_api import router as paper_router
    app.include_router(paper_router)
    from .audit_api import router as audit_router
    app.include_router(audit_router)
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
    return _server


@asynccontextmanager
async def mcp_lifespan():
    if _server is None:
        yield
    else:
        async with _server.session_manager.run():
            if maintenance_enabled():
                yield
                return
            from .paper import maintenance
            stop = asyncio.Event()
            worker = asyncio.create_task(maintenance(stop), name='paper-maintenance')
            try:
                yield
            finally:
                stop.set()
                await worker
