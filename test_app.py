import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest

import app
from engine import Engine
from providers import DemoProvider


ROOT = Path(__file__).parent
TOKEN = 'test-only-' + 'x' * 32
LEAD = json.loads((ROOT / 'samples/leads.json').read_text())[0]


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(getattr(app, 'make_server', None)), 'loopback API factory missing')
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Engine(Path(self.temp.name) / 'state.sqlite', DemoProvider())
        self.server = app.make_server(self.engine, TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, data=None, method='POST', token=TOKEN, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        request_headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        request_headers.update(headers or {})
        conn.request(method, path, None if data is None else json.dumps(data), request_headers)
        response = conn.getresponse()
        status, body = response.status, response.read()
        conn.close()
        return status, json.loads(body)

    def raw(self, extra, body=b'{}', path='/dispatch'):
        with socket.create_connection(self.server.server_address, timeout=3) as conn:
            conn.sendall(('POST ' + path + ' HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer ' +
                          TOKEN + '\r\nContent-Type: application/json\r\n' + extra +
                          '\r\n').encode() + body)
            conn.shutdown(socket.SHUT_WR)
            response = b''
            while chunk := conn.recv(4096):
                response += chunk
            return int(response.split(b' ')[1])

    def test_token_required_and_loopback_only(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        self.assertEqual(self.request('/state', method='GET', token='wrong')[0], 401)
        with self.assertRaises(ValueError):
            app.make_server(self.engine, 'short')
        with self.assertRaises(ValueError):
            app.make_server(self.engine, 'x' * 31 + '\n')

    def test_real_import_review_dispatch_and_replayed_events(self):
        self.assertEqual(self.request('/ingest', {'records': [LEAD]})[1][0]['status'], 'imported')
        review = {'email': LEAD['email'], 'decision': 'approve', 'reviewer': 'Demo Reviewer',
                  'owner_id': '101', 'note': 'Source and contact checked',
                  'contact_verified': True, 'campaign_id': 'demo-campaign'}
        self.assertEqual(self.request('/review', review)[1]['stage'], 'approved')
        self.assertEqual(len(self.request('/dispatch', {})[1]), 3)
        event = {'event_type': 'reply_received', 'event_id': 'demo-event',
                 'lead_email': LEAD['email'], 'campaign_id': 'demo-campaign',
                 'workspace': 'demo-workspace', 'timestamp': '2026-09-30T10:00:00Z',
                 'reply_text': 'Please call', 'email_id': 'demo-email',
                 'campaign_name': 'Demo', 'email_account': 'sender@example.com',
                 'custom_lead_field': {'company_context': 'Custom provider field'}}
        self.assertEqual(self.request('/events', event)[1]['status'], 'recorded')
        self.assertEqual(self.request('/events', event)[1]['status'], 'duplicate')
        self.assertEqual(self.request('/state', method='GET')[1]['leads'][0]['stage'], 'replied')

    def test_reconciliation_routes_to_durable_engine(self):
        self.engine.ingest([LEAD])
        self.engine.review(LEAD['email'], 'approve', 'Demo', '101', 'Checked', True)
        key = 'contact:' + LEAD['email']
        with self.engine.database() as db:
            db.execute("UPDATE operations SET status='uncertain' WHERE key=?", (key,))
        self.assertEqual(self.request('/reconcile', {'key': key, 'remote_id': 'verified-id'})[1]['status'], 'done')
        self.assertEqual(self.request('/reconcile', {'key': key, 'retry': True})[0], 400)

    def test_invalid_shapes_fields_routes_and_methods(self):
        for route, body in [('/dispatch', []), ('/dispatch', {'unknown': 1}),
                            ('/review', {}), ('/ingest', {}), ('/ingest', {'records': 'bad'}),
                            ('/events', {'secret_field': 1}),
                            ('/reconcile', {'key': 'missing', 'retry': 'true'})]:
            with self.subTest(route=route, body=body):
                self.assertEqual(self.request(route, body)[0], 400)
        self.assertEqual(self.request('/missing', {})[0], 404)
        self.assertEqual(self.request('/state', {})[0], 405)
        self.assertEqual(self.request('/dispatch', method='GET')[0], 405)
        self.assertEqual(self.request('/dispatch', {}, method='PUT')[0], 405)
        self.assertEqual(self.request('/dispatch', {}, headers={'Content-Type': 'text/plain'})[0], 415)

    def test_framing_and_strict_json(self):
        for extra, body, status in [('', b'', 411), ('Content-Length: -1\r\n', b'', 400),
            ('Content-Length: 999999999\r\n', b'', 413),
            ('Content-Length: 2\r\nContent-Length: 2\r\n', b'{}', 400),
            ('Transfer-Encoding: chunked\r\n', b'0\r\n\r\n', 400),
            ('Content-Length: 8\r\n', b'{}', 400),
            ('Content-Length: 1\r\n', b'{', 400),
            ('Content-Length: 3\r\n', b'NaN', 400),
            ('Content-Length: 15\r\n', b'{"x":Infinity}', 400),
            ('Content-Length: 2\r\n', b'\xff\xff', 400),
            ('Content-Length: 15\r\n', b'{"x":1,"x":2}', 400)]:
            with self.subTest(extra=extra, body=body):
                self.assertEqual(self.raw(extra, body), status)

    def test_generic_errors_and_no_request_logging(self):
        for error in (RuntimeError, TypeError):
            def fail():
                raise error('secret PII and provider credentials')
            self.engine.snapshot = fail
            captured = io.StringIO()
            with contextlib.redirect_stderr(captured):
                status, body = self.request('/state', method='GET')
            self.assertEqual(status, 500)
            self.assertEqual(body, {'error': 'Internal server error'})
            self.assertEqual(captured.getvalue(), '')

    def test_early_rejections_preserve_http_response_when_client_sends_body_separately(self):
        for _ in range(500):
            self.assertEqual(self.request('/missing', {'note': 'body'})[0], 404)

    def test_nonfinite_values_and_duplicate_fields_are_rejected_before_engine(self):
        for body in (b'{"records":[NaN]}', b'{"records":[Infinity]}',
                     b'{"records":[-Infinity]}', b'{"records":[],"records":[]}'):
            self.assertEqual(self.raw(f'Content-Length: {len(body)}\r\n', body, '/ingest'), 400)

    def test_body_limit_boundary_and_timeout(self):
        body = b'{}' + b' ' * (256 * 1024 - 2)
        self.assertEqual(self.raw(f'Content-Length: {len(body)}\r\n', body), 200)
        old_timeout = app.REQUEST_TIMEOUT
        app.REQUEST_TIMEOUT = 0.05
        try:
            with socket.create_connection(self.server.server_address, timeout=3) as conn:
                conn.sendall(('POST /dispatch HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer ' +
                              TOKEN + '\r\nContent-Type: application/json\r\nContent-Length: 10\r\n\r\n').encode())
                self.assertIn(b' 408 ', conn.recv(4096).split(b'\r\n')[0])
        finally:
            app.REQUEST_TIMEOUT = old_timeout

    def test_cli_state_and_requests_use_environment_token_and_preserve_failure_exit(self):
        env = {**os.environ, 'ENGINE_API_TOKEN': TOKEN}
        port = str(self.server.server_port)
        result = subprocess.run(['python', str(ROOT / 'app.py'), 'state', '--port', port],
                                env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['mode'], 'demo')
        body = Path(self.temp.name) / 'body.json'
        body.write_text(json.dumps({'records': [LEAD]}))
        result = subprocess.run(['python', str(ROOT / 'app.py'), 'request', 'ingest', str(body),
                                 '--port', port], env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)[0]['status'], 'imported')
        body.write_text('{"unknown":true}')
        result = subprocess.run(['python', str(ROOT / 'app.py'), 'request', '/dispatch', str(body),
                                 '--port', port], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {'error': 'Invalid request'})
        env.pop('ENGINE_API_TOKEN')
        result = subprocess.run(['python', str(ROOT / 'app.py'), 'state', '--port', port],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(TOKEN, result.stderr)


class WorkflowTests(unittest.TestCase):
    def evaluate(self, expression, data):
        self.assertTrue(expression.startswith('={{') and expression.endswith('}}'))
        script = 'const input=JSON.parse(process.argv[1]); const $json=input.data; ' + \
                 'process.stdout.write(JSON.stringify(eval(input.code)));'
        result = subprocess.run(['node', '-e', script, json.dumps({
            'data': data, 'code': expression[3:-2]})], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_inactive_credential_free_connected_workflows_and_executable_expressions(self):
        workflow_ids, webhook_ids = set(), set()
        for name, route in [('intake', 'ingest'), ('review', 'review'), ('events', 'events'),
                            ('dispatch', 'dispatch')]:
            with self.subTest(name=name):
                self.assertTrue((ROOT / f'n8n-{name}.json').exists(), 'workflow export missing')
                workflow = json.loads((ROOT / f'n8n-{name}.json').read_text())
                self.assertIsInstance(workflow['id'], str)
                self.assertTrue(workflow['id'])
                self.assertNotIn(workflow['id'], workflow_ids)
                workflow_ids.add(workflow['id'])
                self.assertIs(workflow['active'], False)
                nodes = workflow['nodes']
                self.assertTrue(all('credentials' not in n for n in nodes))
                call = next(n for n in nodes if n['type'] == 'n8n-nodes-base.httpRequest')
                self.assertEqual(call['parameters']['url'], f'http://127.0.0.1:8765/{route}')
                self.assertEqual(call['parameters']['method'], 'POST')
                self.assertEqual(call['parameters']['genericAuthType'], 'httpHeaderAuth')
                self.assertEqual(call['parameters']['specifyBody'], 'json')
                response = call['parameters']['options']['response']['response']
                self.assertIs(response['neverError'], True)
                self.assertIs(response['fullResponse'], True)
                first = nodes[0]
                self.assertEqual(workflow['connections'][first['name']]['main'][0][0]['node'], call['name'])
                if name == 'dispatch':
                    self.assertEqual(first['type'], 'n8n-nodes-base.manualTrigger')
                    self.assertEqual(json.loads(call['parameters']['jsonBody']), {})
                    continue
                self.assertEqual(first['parameters']['authentication'], 'headerAuth')
                self.assertIsInstance(first['webhookId'], str)
                self.assertNotIn(first['webhookId'], webhook_ids)
                webhook_ids.add(first['webhookId'])
                self.assertEqual(first['parameters']['httpMethod'], 'POST')
                self.assertEqual(first['parameters']['responseMode'], 'responseNode')
                payload = {'records': [LEAD]} if name == 'intake' else {'note': 'Quoted "text"\nnext'}
                self.assertEqual(json.loads(self.evaluate(call['parameters']['jsonBody'], {'body': payload})), payload)
                reply = next(n for n in nodes if n['type'] == 'n8n-nodes-base.respondToWebhook')
                self.assertEqual(workflow['connections'][call['name']]['main'][0][0]['node'], reply['name'])
                for status in (200, 400, 401, 413, 500):
                    output = {'statusCode': status, 'body': {'error': 'Example'}}
                    self.assertEqual(self.evaluate(reply['parameters']['options']['responseCode'], output), status)
                    self.assertEqual(self.evaluate(reply['parameters']['responseBody'], output), output['body'])


if __name__ == '__main__':
    unittest.main()
