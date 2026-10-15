"""OAuth discovery compatibility without changing the issuer or token audience."""
from starlette.datastructures import MutableHeaders


class DiscoveryHeadersMiddleware:
    def __init__(self, app, public_url: str | None = None):
        self.app = app
        self.public_url = public_url

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        path = scope.get('path', '')
        from .server import active_public_url
        public_url = self.public_url or active_public_url() or ''
        standard = public_url + '/.well-known/oauth-protected-resource/mcp'
        compatible = public_url + '/oauth/resource-metadata'

        async def send_response(message):
            if message['type'] == 'http.response.start':
                headers = MutableHeaders(scope=message)
                if path.startswith(('/oauth/', '/.well-known/oauth-')):
                    headers['Cache-Control'] = 'no-store'
                    headers['Pragma'] = 'no-cache'
                if path == '/mcp' and message['status'] == 401:
                    # RFC 9728 allows the challenge to advertise any metadata
                    # URL. Certificate-only /.well-known proxy locations must
                    # not prevent a client from starting resource discovery.
                    challenge = headers.get('WWW-Authenticate', '')
                    headers['WWW-Authenticate'] = challenge.replace(
                        f'resource_metadata="{standard}"',
                        f'resource_metadata="{compatible}"',
                    )
                    headers['Cache-Control'] = 'no-store'
            await send(message)

        await self.app(scope, receive, send_response)
