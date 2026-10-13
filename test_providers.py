import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
import unittest

from providers import DemoProvider, LiveProvider, ProviderError, http_request


ASSESSMENT = {'fit': 'high', 'intent': 'explicit_request', 'summary': 'Needs management.',
              'evidence': [{'source_id': 'one', 'quote': 'Need property management'}]}
LEAD = {'email': 'jane@example.test', 'company': 'Example', 'first_name': 'Jane',
        'last_name': 'Doe', 'sources': [{'id': 'one', 'text': 'Need property management',
                                       'url': 'https://example.test', 'observed_at': '2026-10-12'}],
        'assessment': ASSESSMENT, 'stage': 'approved', 'approval': {'owner_id': '101'},
        'contact_id': 'contact-1', 'campaign_id': 'campaign-1'}
ENV = {'HUBSPOT_ACCESS_TOKEN': 'hub-secret', 'INSTANTLY_API_KEY': 'instant-secret',
       'GEMINI_API_KEY': 'gem-secret', 'GEMINI_MODEL': 'configured-model',
       'ALLOW_OUTREACH': 'true'}


class HTTPDouble:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, headers, payload))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        status, body = response
        return status, json.dumps(body).encode() if not isinstance(body, bytes) else body


class ProviderTests(unittest.TestCase):
    def test_stdlib_transport_sends_json_auth_and_returns_http_errors(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.headers.get('Authorization'),
                                 json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(201)
                self.end_headers()
                self.wfile.write(b'{"id":"transport-1"}')

            def do_GET(self):
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/contract')
                    self.end_headers()
                    return
                if self.path == '/contract':
                    self.send_response(200)
                    self.end_headers()
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            status, raw = http_request('POST', base + '/contract',
                                       {'Authorization': 'Bearer fictional', 'Content-Type': 'application/json'},
                                       {'email': 'jane@example.test'})
            self.assertEqual((status, json.loads(raw)), (201, {'id': 'transport-1'}))
            self.assertEqual(received, [('/contract', 'Bearer fictional', {'email': 'jane@example.test'})])
            self.assertEqual(http_request('GET', base + '/missing', {}, None)[0], 404)
            self.assertEqual(http_request('GET', base + '/redirect', {'Authorization': 'Bearer fictional'}, None)[0], 302)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def live(self, *responses, env=None):
        http = HTTPDouble(*responses)
        return LiveProvider(http=http, environ=ENV if env is None else env), http

    def test_demo_requires_fixture_and_returns_independent_deterministic_receipts(self):
        provider = DemoProvider()
        assessment = provider.assess(LEAD)
        self.assertEqual(assessment, ASSESSMENT)
        assessment['evidence'].clear()
        self.assertEqual(len(LEAD['assessment']['evidence']), 1)
        receipt = provider.execute('task', LEAD, {'subject': 'Call'})
        self.assertTrue(receipt['id'].startswith('demo-task-'))
        self.assertEqual(receipt, provider.execute('task', LEAD, {'subject': 'Call'}))
        self.assertNotEqual(receipt, provider.execute('task', LEAD, {'subject': 'Reply'}))
        with self.assertRaises(ProviderError):
            provider.assess({'email': 'missing@example.test'})

    def test_contact_lookup_patch_preserves_lifecycle_and_sets_string_properties(self):
        provider, http = self.live((200, {'id': '42'}), (200, {'id': '42'}))
        self.assertEqual(provider.execute('contact', LEAD, {}), {'id': '42'})
        lookup, patch = http.calls
        self.assertEqual(lookup[:2], ('GET', 'https://api.hubapi.com/crm/v3/objects/contacts/jane%40example.test?idProperty=email'))
        self.assertEqual(patch[:2], ('PATCH', 'https://api.hubapi.com/crm/v3/objects/contacts/42'))
        self.assertEqual(patch[2]['Authorization'], 'Bearer hub-secret')
        props = patch[3]['properties']
        self.assertNotIn('lifecyclestage', props)
        self.assertEqual(props['automation_stage'], 'approved')
        self.assertEqual(props['automation_fit'], 'high')
        self.assertEqual(props['hubspot_owner_id'], '101')
        self.assertEqual(json.loads(props['automation_evidence']), [{
            'source_id': 'one', 'quote': 'Need property management',
            'url': 'https://example.test', 'observed_at': '2026-10-12'}])
        self.assertTrue(all(isinstance(v, str) for v in props.values()))

    def test_contact_404_creates_but_other_read_errors_never_create(self):
        provider, http = self.live((404, {'message': 'Not found'}), (201, {'id': 'new'}))
        self.assertEqual(provider.execute('contact', LEAD, {}), {'id': 'new'})
        self.assertEqual(http.calls[1][:2], ('POST', 'https://api.hubapi.com/crm/v3/objects/contacts'))
        provider, http = self.live((401, {'message': 'secret'}))
        with self.assertRaises(ProviderError) as caught:
            provider.execute('contact', LEAD, {})
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(len(http.calls), 1)

    def test_owned_task_is_associated_to_contact(self):
        provider, http = self.live((201, {'id': 'task-1'}))
        payload = {'subject': 'Follow up', 'body': 'Review source', 'due_at': '2026-10-14T00:00:00Z'}
        self.assertEqual(provider.execute('task', LEAD, payload), {'id': 'task-1'})
        method, url, headers, body = http.calls[0]
        self.assertEqual((method, url), ('POST', 'https://api.hubapi.com/crm/v3/objects/tasks'))
        self.assertEqual(body['properties']['hubspot_owner_id'], '101')
        self.assertEqual(body['properties']['hs_timestamp'], payload['due_at'])
        self.assertEqual(body['properties']['hs_task_subject'], payload['subject'])
        self.assertEqual(body['associations'], [{'to': {'id': 'contact-1'}, 'types': [
            {'associationCategory': 'HUBSPOT_DEFINED', 'associationTypeId': 204}]}])

    def test_enrollment_uses_documented_fields_and_requires_outreach_gate(self):
        provider, http = self.live((200, {'id': 'enroll-1'}))
        self.assertEqual(provider.execute('enroll', LEAD, {}), {'id': 'enroll-1'})
        self.assertEqual(http.calls[0][:2], ('POST', 'https://api.instantly.ai/api/v2/leads'))
        self.assertEqual(http.calls[0][2]['Authorization'], 'Bearer instant-secret')
        self.assertEqual(http.calls[0][3], {'email': 'jane@example.test', 'campaign': 'campaign-1',
            'company_name': 'Example', 'first_name': 'Jane', 'last_name': 'Doe', 'skip_if_in_campaign': True})
        for flag in ('false', '', '1'):
            provider, http = self.live(env={**ENV, 'ALLOW_OUTREACH': flag})
            with self.assertRaises(ProviderError):
                provider.execute('enroll', LEAD, {})
            self.assertEqual(http.calls, [])

    def test_suppression_needs_only_email_and_is_allowed_when_outreach_disabled(self):
        provider, http = self.live((200, {'id': 'block-1'}), env={**ENV, 'ALLOW_OUTREACH': 'false'})
        self.assertEqual(provider.execute('suppress', {'email': 'jane@example.test'}, {}), {'id': 'block-1'})
        self.assertEqual(http.calls[0][:2], ('POST', 'https://api.instantly.ai/api/v2/block-lists-entries'))
        self.assertEqual(http.calls[0][3], {'bl_value': 'jane@example.test'})

    def test_missing_credentials_and_model_fail_without_network(self):
        for kind, removed in [('contact', 'HUBSPOT_ACCESS_TOKEN'), ('enroll', 'INSTANTLY_API_KEY'),
                              ('assess', 'GEMINI_API_KEY'), ('assess', 'GEMINI_MODEL')]:
            provider, http = self.live(env={k: v for k, v in ENV.items() if k != removed})
            with self.subTest(removed=removed), self.assertRaises(ProviderError):
                provider.assess(LEAD) if kind == 'assess' else provider.execute(kind, LEAD, {})
            self.assertEqual(http.calls, [])

    def test_write_failures_are_sanitized_and_ambiguity_is_preserved_without_retries(self):
        cases = [(TimeoutError('secret-url'), True), (OSError('secret-token'), True),
                 ((503, {'message': 'secret-body'}), True), ((400, {'message': 'secret-body'}), False),
                 ((200, b'not json secret'), True), ((200, {}), True), ((200, {'id': []}), True)]
        for response, uncertain in cases:
            provider, http = self.live(response)
            with self.assertRaises(ProviderError) as caught:
                provider.execute('task', LEAD, {'subject': 'x', 'body': 'y', 'due_at': '2026-10-14T00:00:00Z'})
            self.assertEqual(caught.exception.uncertain, uncertain)
            self.assertNotIn('secret', str(caught.exception))
            self.assertEqual(len(http.calls), 1)

    def test_gemini_structured_assessment_only_receives_supplied_sources(self):
        response = {'candidates': [{'content': {'parts': [{'text': json.dumps(ASSESSMENT)}]}, 'finishReason': 'STOP'}]}
        provider, http = self.live((200, response))
        self.assertEqual(provider.assess(LEAD), ASSESSMENT)
        method, url, headers, body = http.calls[0]
        self.assertEqual(url, 'https://generativelanguage.googleapis.com/v1beta/models/configured-model:generateContent')
        self.assertEqual(headers['x-goog-api-key'], 'gem-secret')
        self.assertEqual(body['generationConfig']['responseMimeType'], 'application/json')
        self.assertEqual(body['generationConfig']['responseJsonSchema']['required'], ['fit', 'intent', 'summary', 'evidence'])
        self.assertNotIn('tools', body)
        self.assertNotIn('jane@example.test', json.dumps(body))
        self.assertIn('Need property management', body['contents'][0]['parts'][0]['text'])

    def test_gemini_rejects_invented_quotes_enums_and_truncated_output(self):
        for assessment, finish in [({**ASSESSMENT, 'fit': 'excellent'}, 'STOP'),
            ({**ASSESSMENT, 'evidence': []}, 'STOP'),
            ({**ASSESSMENT, 'evidence': [{'source_id': 'one', 'quote': 'invented'}]}, 'STOP'),
            (ASSESSMENT, 'MAX_TOKENS')]:
            response = {'candidates': [{'content': {'parts': [{'text': json.dumps(assessment)}]}, 'finishReason': finish}]}
            provider, http = self.live((200, response))
            with self.assertRaises(ProviderError) as caught:
                provider.assess(LEAD)
            self.assertFalse(caught.exception.uncertain)


if __name__ == '__main__':
    unittest.main()
