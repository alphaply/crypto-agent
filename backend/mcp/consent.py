"""Browser consent with durable completion and a recoverable callback handoff."""
from __future__ import annotations

import html
import json
import secrets
import time
from urllib.parse import urlencode, urlparse

from mcp.server.auth.provider import construct_redirect_uri
from starlette.responses import HTMLResponse, RedirectResponse

from . import store
from .settings import profiles, settings


def _cookie_name(transaction):
    # Independent OAuth tabs must not replace each other's CSRF cookie.
    return 'mcp_consent_' + store.digest(transaction)[:24]


def _cookie_matches(request, transaction, pending):
    cookie = request.cookies.get(_cookie_name(transaction), '')
    if not pending.get('csrf_token'):  # Forms opened before this deployment.
        cookie = cookie or request.cookies.get('mcp_consent', '')
    return bool(cookie and secrets.compare_digest(store.digest(cookie), pending.get('csrf', '')))


def _page(title, body, *, status=200, pending=None):
    # Chromium applies form-action to POST redirect destinations too. Only the
    # SDK-validated callback origin may receive this form's redirect.
    destination = ''
    if pending:
        parsed = urlparse(pending['params']['redirect_uri'])
        destination = f' {parsed.scheme}://{parsed.netloc}'
    return HTMLResponse(f'''<!doctype html><html lang="zh"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
        <style>body{{font:16px system-ui;background:#f5f7fb;color:#14233b;max-width:540px;margin:7vh auto;padding:24px}}
        form,main{{display:grid;gap:16px;background:white;padding:28px;border-radius:18px}}label{{display:block}}
        input[type=password]{{padding:12px;max-width:100%;box-sizing:border-box}}button,.action{{padding:12px}}
        .error{{color:#a31919}}a{{color:#174fbd}}p{{line-height:1.6;margin:0}}</style></head>
        <body><h1>{html.escape(title)}</h1>{body}</body></html>''', status_code=status, headers={
            'Cache-Control': 'no-store', 'Pragma': 'no-cache', 'Referrer-Policy': 'no-referrer',
            'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
                                       + destination + "; frame-ancestors 'none'; base-uri 'none'",
        })


def _expired(provider, pending=None):
    restart = ''
    if pending:
        params = pending['params']
        query = {'client_id': pending['client_id'], 'response_type': 'code',
                 'redirect_uri': params['redirect_uri'], 'code_challenge': params['code_challenge'],
                 'code_challenge_method': 'S256', 'scope': ' '.join(params.get('scopes') or ['read'])}
        query.update({key: params[key] for key in ('state', 'resource') if params.get(key) is not None})
        url = provider.public_url + '/oauth/authorize?' + urlencode(query)
        restart = f'<a class="action" href="{html.escape(url, quote=True)}">重新打开授权页面</a>'
    return _page('授权请求已过期', '<main><p>此授权页面已失效，未创建新的访问权限。</p>'
                 '<p>请返回 ChatGPT 或其他发起连接的客户端，重新点击连接。不要重复提交旧页面。</p>'
                 + restart + '<p>如果客户端仍在等待，可以重新打开授权页面；若客户端也已超时，请从客户端重新发起。</p>'
                 '<a href="/">返回 Crypto Agent</a></main>', status=400)


def _record_event(transaction, pending, status):
    store.audit('oauth', 'oauth_consent', status, result={
        'transaction_ref': store.digest(transaction)[:12],
        'client_id': pending.get('client_id') if pending else None,
    })


def _redirect(url):
    return RedirectResponse(url, status_code=303, headers={
        'Cache-Control': 'no-store', 'Pragma': 'no-cache', 'Referrer-Policy': 'no-referrer',
    })


def _completed(provider, request, transaction, pending, *, resumed=True):
    from .auth import decrypt
    # Do not disclose an authorization code to a browser that merely knows a
    # completed consent URL. Keep the original cookie for bounded recovery.
    if _cookie_matches(request, transaction, pending):
        code_digest = pending.get('code_digest')
        code = store.get('code', code_digest) if code_digest else None
        if pending['completed_expires_at'] > time.time() and (not code_digest or (code and code['expires_at'] > time.time())):
            if resumed:
                _record_event(transaction, pending, 'callback_reissued')
            return _redirect(decrypt(pending['callback']))
        if code and code['expires_at'] <= time.time():
            return _expired(provider, pending)
    return _page('授权已经处理', '<main><p>授权结果已经提交。请返回发起连接的客户端查看连接状态。</p>'
                 '<p>若客户端仍未连接，请从客户端重新发起授权；刷新此页面不会再创建授权码。</p>'
                 '<a href="/">返回 Crypto Agent</a></main>')


def _form(provider, transaction, pending, *, error='', status=200, submitted=None):
    from .auth import decrypt
    esc = html.escape
    if not pending.get('csrf_token'):
        link = '/oauth/consent?' + urlencode({'transaction': transaction})
        return _page('请重新打开授权页面', '<main><p>授权服务已更新，请重新打开页面后继续。</p>'
                     f'<a href="{esc(link, quote=True)}">重新打开授权页面</a></main>', status=status)
    csrf = decrypt(pending['csrf_token'])
    requested = list(dict.fromkeys(['read'] + (pending['params'].get('scopes') or [])))
    selected = set(submitted.getlist('scopes')) if submitted is not None else set(requested)
    selected_profiles = set(submitted.getlist('profile_ids')) if submitted is not None else set()
    scope_options = ''.join(f'<label><input type="checkbox" name="scopes" value="{esc(scope, quote=True)}"'
                            + (' checked' if scope in selected else '') + f'>{esc(scope)}</label>'
                            for scope in requested if scope != 'read')
    options = ''.join(f'<label><input type="checkbox" name="profile_ids" value="{esc(p["profile_id"], quote=True)}"'
                      + (' checked' if p['profile_id'] in selected_profiles else '') + f'>{esc(p["name"])}</label>'
                      for p in profiles() if p.get('enabled'))
    notice = f'<p role="alert" class="error">{esc(error)}</p>' if error else ''
    response = _page('连接 Crypto Agent', f'''<form method="post" action="/oauth/consent">
        {notice}<p>客户端：{esc(pending['client_name'])}</p><p>查询权限 read；选择额外授权：</p>{scope_options}
        <p>选择允许访问的 MCP 交易配置：</p>{options}
        <input type="hidden" name="transaction" value="{esc(transaction, quote=True)}">
        <input type="hidden" name="csrf" value="{esc(csrf, quote=True)}">
        <label>管理员密码 <input name="password" type="password" autocomplete="current-password" required></label>
        <button name="decision" value="allow">授权连接</button><button name="decision" value="deny" formnovalidate>取消</button>
        <p>授权后会返回客户端。若没有返回，请刷新本页恢复跳转。</p></form>''', status=status, pending=pending)
    response.set_cookie(_cookie_name(transaction), csrf, httponly=True,
                        secure=provider.public_url.startswith('https:'), samesite='lax',
                        max_age=max(1, int(pending['expires_at'] - time.time())), path='/oauth/consent')
    return response


def _prepare(transaction):
    """Atomically initialize stable CSRF for older, still-valid pending rows."""
    from .auth import encrypt
    key = store.digest(transaction)
    with store.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT payload FROM mcp_records WHERE kind='pending' AND id=?", (key,)).fetchone()
        pending = json.loads(row[0]) if row else None
        if pending and not pending.get('completed_at') and not pending.get('csrf_token') and pending['expires_at'] > time.time():
            csrf = secrets.token_urlsafe(32)
            pending.update(csrf=store.digest(csrf), csrf_token=encrypt(csrf))
            conn.execute("UPDATE mcp_records SET payload=? WHERE kind='pending' AND id=?", (json.dumps(pending), key))
        conn.commit()
    return pending


def _complete(provider, transaction, pending, *, scopes=None, profile_ids=None):
    """One transaction records consent, code and audit; retries reuse the result."""
    from .auth import MCPCode, encrypt
    key = store.digest(transaction)
    with store.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT payload FROM mcp_records WHERE kind='pending' AND id=?", (key,)).fetchone()
        current = json.loads(row[0]) if row else None
        if not current or current['expires_at'] <= time.time():
            return None
        if current.get('completed_at'):
            return current
        if not secrets.compare_digest(current.get('csrf', ''), pending.get('csrf', '')):
            return None
        params, now = current['params'], time.time()
        callback_fields = {'state': params.get('state')}
        if scopes is not None:
            code, grant = secrets.token_urlsafe(32), secrets.token_hex(16)
            record = MCPCode(code='', client_id=current['client_id'], scopes=scopes, profile_ids=profile_ids,
                             grant_id=grant, expires_at=now + 120, code_challenge=params['code_challenge'],
                             redirect_uri=params['redirect_uri'], redirect_uri_provided_explicitly=params['redirect_uri_provided_explicitly'],
                             resource=provider.resource, subject='admin').model_dump(mode='json')
            record.pop('code')
            current['code_digest'] = store.digest(code)
            conn.execute('INSERT INTO mcp_records VALUES(?,?,?)', ('code', current['code_digest'], json.dumps(record)))
            callback_fields['code'] = code
        else:
            callback_fields['error'] = 'access_denied'
        current.update(completed_at=now, completed_expires_at=now + 120,
                       callback=encrypt(construct_redirect_uri(params['redirect_uri'], **callback_fields)))
        conn.execute("UPDATE mcp_records SET payload=? WHERE kind='pending' AND id=?", (json.dumps(current), key))
        conn.execute('INSERT INTO mcp_audit(timestamp,principal,tool,status,result) VALUES(?,?,?,?,?)',
                     (now, 'admin', 'oauth_authorize', 'completed' if scopes is not None else 'denied',
                      json.dumps({'client_id': current['client_id'], 'scopes': scopes, 'profile_ids': profile_ids,
                                  'transaction_ref': key[:12]})))
        conn.commit()
    return current


async def handle_consent(provider, request):
    from backend.app.core.security import authenticate_password, check_login_allowed, record_login_failure, reset_login_attempts
    from .auth import validate_grant
    form = await request.form() if request.method == 'POST' else None
    transaction = str(form.get('transaction', '')) if form is not None else request.query_params.get('transaction', '')
    pending = _prepare(transaction) if request.method == 'GET' else store.get('pending', store.digest(transaction))
    if not pending or pending['expires_at'] <= time.time():
        _record_event(transaction, pending, 'expired' if pending else 'missing')
        return _expired(provider, pending)
    if not settings()['enabled']:
        return _page('MCP 已停用', '<main><p>管理员已停用 MCP，请返回客户端取消连接。</p></main>', status=403)
    if pending.get('completed_at'):
        return _completed(provider, request, transaction, pending)
    if request.method == 'GET':
        return _form(provider, transaction, pending)
    csrf = str(form.get('csrf', ''))
    if not csrf or not _cookie_matches(request, transaction, pending) or not secrets.compare_digest(store.digest(csrf), pending.get('csrf', '')):
        _record_event(transaction, pending, 'csrf_failed')
        link = '/oauth/consent?' + urlencode({'transaction': transaction})
        return _page('授权页面需要刷新', '<main><p>浏览器未提供此授权页的有效 Cookie，或页面已更新。请允许本站 Cookie 后重新打开授权页面。</p>'
                     f'<a href="{html.escape(link, quote=True)}">重新打开授权页面</a></main>', status=403)
    if form.get('decision') == 'deny':
        completed = _complete(provider, transaction, pending)
        return _completed(provider, request, transaction, completed, resumed=False) if completed else _expired(provider, pending)
    if form.get('decision') != 'allow':
        return _form(provider, transaction, pending, error='请选择授权或取消。', status=400, submitted=form)
    ip = request.client.host if request.client else 'unknown'
    allowed, reason = check_login_allowed(ip)
    if not allowed:
        return _form(provider, transaction, pending, error=reason, status=429, submitted=form)
    if not authenticate_password(str(form.get('password', ''))):
        record_login_failure(ip)
        return _form(provider, transaction, pending, error='管理员密码不正确，请重新输入。', status=401, submitted=form)
    reset_login_attempts(ip)
    selected_scopes = [str(value) for value in form.getlist('scopes')]
    if not set(selected_scopes) <= set(pending['params'].get('scopes') or []):
        return _form(provider, transaction, pending, error='不能授予客户端未申请的权限。', status=400, submitted=form)
    scopes = list(dict.fromkeys(['read'] + selected_scopes))
    profile_ids = list(dict.fromkeys(str(value) for value in form.getlist('profile_ids')))
    try:
        validate_grant(scopes, profile_ids)
    except ValueError as exc:
        return _form(provider, transaction, pending, error=str(exc), status=400, submitted=form)
    completed = _complete(provider, transaction, pending, scopes=scopes, profile_ids=profile_ids)
    return _completed(provider, request, transaction, completed, resumed=False) if completed else _expired(provider, pending)
