"""Optional real-Chromium check: HTTP clients do not enforce form-action CSP."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

import pytest
from websockets.sync.client import connect

from backend.mcp.consent import _page


def _chrome():
    candidates = [os.getenv('CHROME_BINARY'), shutil.which('google-chrome'), shutil.which('chromium'),
                  r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                  r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe']
    return next((str(path) for path in candidates if path and Path(path).is_file()), None)


@pytest.mark.skipif(not _chrome(), reason='A local Chromium browser is required for CSP navigation verification')
def test_real_browser_cross_origin_form_redirect(tmp_path):
    received = []

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/callback':
                received.append(self.path)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<h1>Callback reached</h1>')

        def log_message(self, *args):
            pass

    callback = ThreadingHTTPServer(('127.0.0.1', 0), Callback)
    destination = f'http://127.0.0.1:{callback.server_port}/callback'

    class Consent(BaseHTTPRequestHandler):
        def do_GET(self):
            page = _page('Consent', '<form method="post" action="/submit"><button>Allow</button></form>',
                         pending={'params': {'redirect_uri': destination}})
            self.send_response(200)
            for name, value in page.headers.items():
                if name == 'content-security-policy' and self.path == '/old':
                    value = value.replace(' ' + destination.rsplit('/', 1)[0], '')
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(page.body)

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            self.send_response(303)
            self.send_header('Location', destination)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def log_message(self, *args):
            pass

    consent = ThreadingHTTPServer(('127.0.0.1', 0), Consent)
    for server in (callback, consent):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tmp_path / 'browser-profile'
    args = [_chrome(), '--headless=new', '--no-first-run', '--no-default-browser-check',
            '--no-proxy-server', '--remote-debugging-port=0', f'--user-data-dir={profile}', 'about:blank']
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        port_file = profile / 'DevToolsActivePort'
        deadline = time.monotonic() + 15
        while not port_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert port_file.exists(), 'Isolated headless browser did not start'
        port = port_file.read_text().splitlines()[0]
        with urlopen(f'http://127.0.0.1:{port}/json/list', timeout=5) as response:
            target = next(page for page in json.load(response) if page['type'] == 'page')
        with connect(target['webSocketDebuggerUrl'], open_timeout=5) as websocket:
            sequence = 0

            def cdp(method, params=None):
                nonlocal sequence
                sequence += 1
                websocket.send(json.dumps({'id': sequence, 'method': method, 'params': params or {}}))
                while True:
                    message = json.loads(websocket.recv(timeout=5))
                    if message.get('id') == sequence:
                        return message.get('result', {})

            def evaluate(expression):
                return cdp('Runtime.evaluate', {'expression': expression, 'returnByValue': True}).get('result', {}).get('value')

            def wait_for(expression):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if evaluate(expression):
                        return
                    time.sleep(0.05)
                pytest.fail('Browser did not reach expected page: ' + str(evaluate('location.href')))

            for mode in ('old', 'fixed'):
                cdp('Page.navigate', {'url': f'http://127.0.0.1:{consent.server_port}/{mode}'})
                wait_for("!!document.querySelector('button')")
                evaluate("document.querySelector('button').click()")
                if mode == 'old':
                    time.sleep(0.5)
                    assert received == [], 'Old restrictive CSP unexpectedly allowed the external form redirect'
                else:
                    wait_for("document.body.innerText.includes('Callback reached')")
                    assert received == ['/callback']
    finally:
        process.terminate()
        process.wait(timeout=10)
        for server in (callback, consent):
            server.shutdown()
            server.server_close()
