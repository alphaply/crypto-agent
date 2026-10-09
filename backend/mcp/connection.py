"""Admin connection setup and bounded, credential-free OAuth diagnostics."""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlparse

import httpx
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.settings import ClientRegistrationOptions
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import AuthorizationProvider
from .settings import SCOPES, settings


def connection_details(public_url: str, active_public_url: str | None = None) -> dict:
    return {
        'url': public_url + '/mcp', 'transport': 'streamable-http',
        'active_public_url': active_public_url,
        'oauth_issuer': public_url + '/oauth',
        'oauth_metadata': public_url + '/.well-known/oauth-authorization-server/oauth',
        'resource_metadata': public_url + '/.well-known/oauth-protected-resource/mcp',
        'resource_metadata_fallback': public_url + '/oauth/resource-metadata',
        'authorization_endpoint': public_url + '/oauth/authorize',
        'token_endpoint': public_url + '/oauth/token',
        'registration_endpoint': public_url + '/oauth/register',
        'resource': public_url + '/mcp',
        'restart_required': bool(active_public_url and active_public_url != public_url),
        'configuration': {'mcpServers': {'crypto-agent': {
            'url': public_url + '/mcp', 'headers': {'Authorization': 'Bearer <API_KEY>'},
        }}},
    }


class ChatGPTClientRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default='ChatGPT', min_length=1, max_length=100)
    redirect_uri: str = Field(min_length=1, max_length=2000)
    scopes: list[str] = Field(default_factory=lambda: ['read'], min_length=1, max_length=3)

    @field_validator('redirect_uri')
    @classmethod
    def valid_redirect(cls, value):
        parsed = urlparse(value)
        if (parsed.scheme != 'https' or parsed.hostname not in {'chatgpt.com', 'chat.openai.com'}
                or parsed.port not in {None, 443} or parsed.username or parsed.password
                or parsed.fragment or parsed.path in {'', '/'}):
            raise ValueError('请粘贴 ChatGPT 页面提供的完整 HTTPS 回调地址')
        return value

    @field_validator('scopes')
    @classmethod
    def valid_scopes(cls, value):
        if 'read' not in value or not set(value) <= set(SCOPES):
            raise ValueError('Scopes must include read and may include trade/cancel')
        return list(dict.fromkeys(value))


async def register_chatgpt_client(payload: ChatGPTClientRequest, public_url: str) -> dict:
    """Use the same SDK registration handler as DCR; this does not grant access."""
    class RegistrationRequest:
        async def json(self):
            return {
                'client_name': payload.name, 'redirect_uris': [payload.redirect_uri],
                'token_endpoint_auth_method': 'client_secret_post',
                'grant_types': ['authorization_code', 'refresh_token'],
                'response_types': ['code'], 'scope': ' '.join(payload.scopes),
            }

    result = await RegistrationHandler(AuthorizationProvider(public_url), ClientRegistrationOptions(
        enabled=True, valid_scopes=SCOPES, default_scopes=['read'],
    )).handle(RegistrationRequest())
    if result.status_code != 201:
        raise ValueError('OAuth 客户端注册失败；请检查回调地址和客户端数量')
    return json.loads(result.body)


async def check_connection(public_url: str, active_public_url: str | None) -> dict:
    expected = connection_details(public_url, active_public_url)
    checks = []

    def add(key, label, status, message, url=None):
        checks.append({'id': key, 'label': label, 'status': status, 'message': message,
                       **({'url': url} if url else {})})

    add('active_origin', '服务公开地址', 'error' if expected['restart_required'] else 'ok',
        '保存的公开地址尚未应用，请重启后端容器' if expected['restart_required'] else '运行中的公开地址与保存值一致')
    add('enabled', 'MCP 开关', 'ok' if settings()['enabled'] else 'error',
        'MCP 已启用' if settings()['enabled'] else '请先启用 MCP')
    parsed = urlparse(public_url)
    if parsed.hostname in {'localhost', '127.0.0.1', '::1'} or parsed.scheme != 'https':
        add('https', '公网 HTTPS', 'error', 'ChatGPT 网页版需要可访问的 HTTPS 公网域名')
        return {'success': True, 'ready': False, 'checks': checks, 'checked_at': time.time()}

    paths = {'resource': expected['resource_metadata'], 'authorization': expected['oauth_metadata'],
             'resource_fallback': expected['resource_metadata_fallback'],
             'authorization_fallback': public_url + '/oauth/.well-known/oauth-authorization-server',
             'mcp': expected['url']}
    async with httpx.AsyncClient(timeout=6.0, follow_redirects=False) as client:
        async def fetch(key, url, fresh=True):
            try:
                options = {'headers': {'Accept': 'application/json'}}
                if fresh:
                    options['params'] = {'_mcp_check': str(time.time_ns())}
                    options['headers']['Cache-Control'] = 'no-cache'
                response = await client.get(url, **options)
                return key, response
            except httpx.HTTPError:
                return key, None
        # Check both the origin and the exact URLs a connecting client sees;
        # cache-busting alone can hide stale localhost metadata at the proxy.
        responses = dict(await asyncio.gather(
            *(fetch(key, url) for key, url in paths.items()),
            *(fetch(key + '_cached', url, False) for key, url in paths.items() if key != 'mcp'),
        ))

    labels = {'resource': '标准资源发现', 'authorization': '标准 OAuth 发现',
              'resource_fallback': '兼容资源发现', 'authorization_fallback': '兼容 OAuth 发现'}
    for key, label in labels.items():
        response = responses[key]
        if response is None:
            add(key, label, 'error', '连接失败或超时，请检查域名、证书和反向代理', paths[key])
            continue
        try:
            body = response.json() if len(response.content) <= 65536 else None
        except ValueError:
            body = None
        if response.status_code != 200 or not isinstance(body, dict):
            add(key, label, 'error', f'HTTP {response.status_code}，未返回发现 JSON；请将此路径转发后端，排除静态页面和证书验证规则拦截', paths[key])
            continue
        if key.startswith('resource'):
            valid = body.get('resource') == expected['resource'] and body.get('authorization_servers') == [expected['oauth_issuer']]
        else:
            valid = (body.get('issuer') == expected['oauth_issuer']
                     and all(body.get(field) == expected[field] for field in ('authorization_endpoint', 'token_endpoint', 'registration_endpoint'))
                     and isinstance(body.get('code_challenge_methods_supported'), list)
                     and 'S256' in body['code_challenge_methods_supported'])
        add(key, label, 'ok' if valid else 'error',
            '发现配置正确' if valid else '发现配置与当前公网域名不一致或缺少 PKCE/DCR；请重启并清除代理缓存', paths[key])
        if valid:
            cached = responses[key + '_cached']
            try:
                cached_body = cached.json() if cached is not None and len(cached.content) <= 65536 else None
            except ValueError:
                cached_body = None
            same = cached is not None and cached.status_code == 200 and cached_body == body
            add(key + '_cache', label + '缓存', 'ok' if same else 'error',
                '客户端直接访问与最新发现配置一致' if same else '普通 URL 与最新配置不一致或无法访问；请清除该地址的代理/CDN 缓存后重试', paths[key])

    response = responses['mcp']
    challenge = response.headers.get('www-authenticate', '') if response is not None else ''
    correct_challenge = any(f'resource_metadata="{expected[field]}"' in challenge
                            for field in ('resource_metadata', 'resource_metadata_fallback'))
    valid = response is not None and response.status_code == 401 and correct_challenge
    add('mcp', 'MCP 授权入口', 'ok' if valid else 'error',
        '已返回正确的 OAuth 授权提示' if valid else 'MCP 未返回预期的 401 与资源发现地址；请检查 Host 转发、MCP 路由和公开地址', expected['url'])
    return {'success': True, 'ready': all(item['status'] == 'ok' for item in checks),
            'checks': checks, 'checked_at': time.time()}
