"""Authenticated loopback API for the lead automation engine."""

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from engine import Engine
from providers import DemoProvider, LiveProvider, _NoRedirect


MAX_BODY = 256 * 1024
REQUEST_TIMEOUT = 10
ROUTES = {'/state', '/ingest', '/review', '/events', '/dispatch', '/reconcile'}


def require_token(token):
    if not isinstance(token, str) or len(token) < 32 or not re.fullmatch(r'[!-~]+', token):
        raise ValueError('ENGINE_API_TOKEN must contain at least 32 printable ASCII characters without spaces')
    return token


def strict_json(raw):
    def reject_constant(value):
        raise ValueError('Invalid JSON constant')

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON field')
            result[key] = value
        return result

    return json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_object)


def make_server(engine, token, port=0):
    token = require_token(token)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(REQUEST_TIMEOUT)
            self.body_read = False

        def log_message(self, format, *args):
            pass

        def send_error(self, code, message=None, explain=None):
            self.respond(code, {'error': 'Invalid HTTP request'})

        def respond(self, status, value):
            lengths = self.headers.get_all('Content-Length', []) if hasattr(self, 'headers') else []
            if (not self.body_read and len(lengths) == 1 and re.fullmatch(r'[0-9]{1,12}', lengths[0])
                    and int(lengths[0]) <= MAX_BODY and self.headers.get('Transfer-Encoding') is None):
                # Drain bounded rejected bodies so closing TCP does not reset the response on Windows.
                self.body_read = True
                try:
                    self.rfile.read(int(lengths[0]))
                except OSError:
                    pass
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            self.wfile.write(raw)

        def handle_route(self):
            auth = self.headers.get_all('Authorization', [])
            if len(auth) != 1 or not hmac.compare_digest(auth[0].encode(), ('Bearer ' + token).encode()):
                self.respond(401, {'error': 'Unauthorized'})
                return
            if self.path not in ROUTES:
                self.respond(404, {'error': 'Unknown route'})
                return
            expected_method = 'GET' if self.path == '/state' else 'POST'
            if self.command != expected_method:
                self.respond(405, {'error': 'Method not allowed'})
                return
            try:
                if self.command == 'GET':
                    result = engine.snapshot()
                else:
                    if self.headers.get('Transfer-Encoding') is not None:
                        self.respond(400, {'error': 'Transfer encoding is unsupported'})
                        return
                    lengths = self.headers.get_all('Content-Length', [])
                    if not lengths:
                        self.respond(411, {'error': 'Content-Length required'})
                        return
                    if len(lengths) != 1 or not re.fullmatch(r'[0-9]+', lengths[0]) or len(lengths[0]) > 12:
                        self.respond(400, {'error': 'Invalid Content-Length'})
                        return
                    length = int(lengths[0])
                    if length > MAX_BODY:
                        self.respond(413, {'error': 'JSON body exceeds 256 KiB'})
                        return
                    types = self.headers.get_all('Content-Type', [])
                    if len(types) != 1 or types[0].split(';')[0].strip().lower() != 'application/json':
                        self.respond(415, {'error': 'Content-Type must be application/json'})
                        return
                    try:
                        self.body_read = True
                        raw = self.rfile.read(length)
                    except TimeoutError:
                        self.respond(408, {'error': 'Request body timeout'})
                        return
                    if len(raw) != length:
                        raise ValueError('Incomplete body')
                    body = strict_json(raw.decode('utf-8'))
                    if not isinstance(body, dict):
                        raise ValueError('JSON body must be an object')
                    result = self.execute(body)
                self.respond(200, result)
            except (ValueError, UnicodeError, RecursionError):
                self.respond(400, {'error': 'Invalid request'})
            except Exception:
                self.respond(500, {'error': 'Internal server error'})

        def execute(self, body):
            if self.path == '/events':
                # Instantly merges arbitrary custom lead fields into webhook payloads.
                return engine.event(body)
            fields = {
                '/ingest': ({'records'}, {'records'}),
                '/review': ({'email', 'decision', 'reviewer', 'owner_id', 'note', 'contact_verified', 'campaign_id'},
                            {'email', 'decision', 'reviewer', 'owner_id', 'note'}),
                '/dispatch': (set(), set()),
                '/reconcile': ({'key', 'remote_id', 'retry'}, {'key'})}
            allowed, required = fields[self.path]
            if not required <= body.keys() or body.keys() - allowed:
                raise ValueError('Unexpected or missing fields')
            if self.path == '/ingest':
                return engine.ingest(body['records'])
            if self.path == '/review':
                return engine.review(**body)
            if self.path == '/reconcile':
                return engine.reconcile(**body)
            return engine.dispatch()

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_route

    # ponytail: serialized HTTP requests; leased jobs before multi-worker throughput.
    return HTTPServer(('127.0.0.1', port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    serve = commands.add_parser('serve')
    serve.add_argument('--mode', choices=('demo', 'live'), default='demo')
    serve.add_argument('--db', type=Path)
    request = commands.add_parser('request')
    request.add_argument('route', choices=('ingest', 'review', 'events', 'dispatch', 'reconcile',
                                           '/ingest', '/review', '/events', '/dispatch', '/reconcile'))
    request.add_argument('file', type=Path)
    state = commands.add_parser('state')
    for command in (serve, request, state):
        command.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        token = require_token(os.environ.get('ENGINE_API_TOKEN', ''))
        if args.command == 'serve':
            db = args.db or Path(__file__).parent / 'output' / f'{args.mode}.sqlite'
            provider = DemoProvider() if args.mode == 'demo' else LiveProvider()
            with make_server(Engine(db, provider), token, args.port) as server:
                print(f'{args.mode} API listening on http://127.0.0.1:{server.server_port}', flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
            return 0
        headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        if args.command == 'request':
            raw = args.file.read_bytes()
            if len(raw) > MAX_BODY or not isinstance(strict_json(raw.decode('utf-8')), dict):
                raise ValueError('Request file must contain a JSON object of at most 256 KiB')
            route, method = '/' + args.route.lstrip('/'), 'POST'
        else:
            raw, route, method = None, '/state', 'GET'
        request = Request(f'http://127.0.0.1:{args.port}{route}', data=raw, headers=headers, method=method)
        try:
            with build_opener(_NoRedirect()).open(request, timeout=30) as response:
                result = response.read()
        except HTTPError as error:
            with error:
                result = error.read()
            print(result.decode('utf-8'))
            return 1
        print(result.decode('utf-8'))
        return 0
    except (OSError, ValueError, UnicodeError, RecursionError, URLError):
        print('Check local API configuration, input JSON, and server availability.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
