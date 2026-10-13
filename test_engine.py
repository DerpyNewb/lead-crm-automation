import copy
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest

from engine import Engine


SAMPLES = json.loads((Path(__file__).parent / 'samples/leads.json').read_text())


class RecordingProvider:
    mode = 'demo'

    def __init__(self):
        self.calls = []
        self.fail_kind = None

    def assess(self, lead):
        return lead['assessment']

    def execute(self, kind, lead, payload):
        self.calls.append((kind, copy.deepcopy(lead), payload))
        if kind == self.fail_kind:
            raise TimeoutError('simulated ambiguous delivery')
        if kind == 'contact':
            return {'id': lead.get('contact_id', 'demo-contact-1')}
        return {'id': f'demo-{kind}-{len(self.calls)}'}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.sqlite'
        self.provider = RecordingProvider()
        self.engine = Engine(self.path, self.provider)
        self.lead = copy.deepcopy(SAMPLES[0])

    def approve(self, campaign_id='demo-campaign'):
        self.engine.ingest([self.lead])
        return self.engine.review(self.lead['email'], 'approve', 'Reviewer', '101',
                                  'Contact and source checked', True, campaign_id)

    def event(self, event_type, event_id='event-1', **overrides):
        return {'event_type': event_type, 'event_id': event_id,
                'lead_email': self.lead['email'], 'campaign_id': 'demo-campaign',
                'workspace': 'demo-workspace', 'timestamp': '2026-09-30T10:00:00Z',
                **overrides}

    def test_import_normalizes_email_and_does_not_overwrite_reviewed_data(self):
        self.approve(None)
        changed = copy.deepcopy(self.lead)
        changed.update(email=' ALEX@NORTHWIND.EXAMPLE ', company='Changed')
        self.engine.ingest([changed])
        state = self.engine.snapshot()
        self.assertEqual(len(state['leads']), 1)
        self.assertEqual(state['leads'][0]['company'], self.lead['company'])
        self.assertEqual(state['leads'][0]['stage'], 'approved')

    def test_bad_records_go_to_review_without_stopping_the_batch(self):
        bad = {**self.lead, 'company': ''}
        result = self.engine.ingest([bad, self.lead])
        self.assertEqual([r['status'] for r in result], ['rejected', 'imported'])
        self.assertEqual(len(self.engine.snapshot()['failures']), 1)

    def test_rejects_invented_evidence_and_unsupported_classifications(self):
        patches = [ {'evidence': [{'source_id': 'invented', 'quote': 'text'}]},
                    {'evidence': [{'source_id': 'company-brief', 'quote': 'invented'}]},
                    {'evidence': []}, {'fit': 'certain'}, {'summary': ''} ]
        for patch in patches:
            with self.subTest(patch=patch):
                lead = copy.deepcopy(self.lead)
                lead['assessment'].update(patch)
                self.assertEqual(self.engine.ingest([lead])[0]['status'], 'rejected')
        self.assertEqual(len(self.engine.snapshot()['leads']), 0)

    def test_rejects_unsafe_or_future_source_metadata(self):
        for patch in [{'url': 'http://127.0.0.1/secret'},
                      {'url': 'https://user:password@example.com'},
                      {'observed_at': '2099-01-01T00:00:00Z'},
                      {'observed_at': 'yesterday'}, {'text': ''}]:
            lead = copy.deepcopy(self.lead)
            lead['sources'][0].update(patch)
            self.assertEqual(self.engine.ingest([lead])[0]['status'], 'rejected')

    def test_approval_requires_human_contact_verification_and_owner(self):
        self.engine.ingest([self.lead])
        for reviewer, owner, verified in [('', '101', True), ('R', '', True),
                                           ('R', '101', False), ('R', '101', 'true')]:
            with self.assertRaises(ValueError):
                self.engine.review(self.lead['email'], 'approve', reviewer, owner,
                                   'Checked', verified)
        self.assertEqual(self.engine.snapshot()['operations'], [])

    def test_low_fit_cannot_be_approved_and_rejection_creates_no_operations(self):
        self.lead['assessment']['fit'] = 'low'
        self.engine.ingest([self.lead])
        with self.assertRaises(ValueError):
            self.engine.review(self.lead['email'], 'approve', 'R', '101', 'Checked', True)
        self.engine.review(self.lead['email'], 'reject', 'R', '101', 'Wrong fit')
        self.assertEqual(self.engine.snapshot()['leads'][0]['stage'], 'rejected')
        self.assertEqual(self.engine.snapshot()['operations'], [])

    def test_approval_is_idempotent_and_dispatch_orders_crm_before_enrollment(self):
        self.approve()
        self.approve()
        self.assertEqual(len(self.engine.snapshot()['operations']), 3)
        self.engine.dispatch()
        self.engine.dispatch()
        self.assertEqual([c[0] for c in self.provider.calls], ['contact', 'task', 'enroll', 'contact'])
        self.assertEqual(self.provider.calls[-1][1]['stage'], 'enrolled')
        lead = self.engine.snapshot()['leads'][0]
        self.assertEqual(lead['stage'], 'enrolled')
        self.assertEqual(lead['contact_id'], 'demo-contact-1')
        self.assertEqual(self.provider.calls[1][1]['contact_id'], 'demo-contact-1')

    def test_reply_replay_creates_one_followup_and_conflicting_ids_fail(self):
        self.approve()
        self.engine.dispatch()
        event = self.event('reply_received')
        self.engine.event(event)
        self.engine.event(event)
        self.engine.dispatch()
        self.assertEqual(sum(c[0] == 'task' for c in self.provider.calls), 2)
        self.assertEqual(len(self.engine.snapshot()['events']), 1)
        with self.assertRaises(ValueError):
            self.engine.event({**event, 'event_type': 'email_bounced'})

    def test_suppression_cancels_unsent_enrollment_and_late_events_do_not_reopen(self):
        for event_type in ['email_bounced', 'lead_unsubscribed']:
            with self.subTest(event_type=event_type):
                self.setUp()
                self.approve()
                self.engine.event(self.event(event_type))
                self.engine.event(self.event('reply_received', 'late-reply'))
                self.engine.event(self.event('email_sent', 'late-sent'))
                self.engine.dispatch()
                state = self.engine.snapshot()
                self.assertEqual(state['leads'][0]['stage'], 'suppressed')
                self.assertNotIn('enroll', [c[0] for c in self.provider.calls])
                self.assertNotIn('task', [c[0] for c in self.provider.calls])
                self.assertIn('suppress', [c[0] for c in self.provider.calls])

    def test_unknown_leads_campaign_mismatch_and_bad_timestamps_are_rejected(self):
        self.approve()
        for patch in [{'lead_email': 'unknown@example.com'},
                      {'campaign_id': 'another-campaign'}, {'timestamp': 'invalid'},
                      {'timestamp': '2099-01-01T00:00:00Z'}]:
            with self.assertRaises(ValueError):
                self.engine.event(self.event('reply_received', **patch))
        self.assertEqual(len(self.engine.snapshot()['events']), 0)
        self.assertEqual(len(self.engine.snapshot()['failures']), 4)

    def test_ambiguous_delivery_blocks_later_work_until_reconciled(self):
        self.approve()
        self.provider.fail_kind = 'task'
        self.engine.dispatch()
        self.assertEqual([c[0] for c in self.provider.calls], ['contact', 'task'])
        self.engine = Engine(self.path, self.provider)
        self.engine.dispatch()
        self.assertEqual(len(self.provider.calls), 2)
        task = next(op for op in self.engine.snapshot()['operations'] if op['kind'] == 'task')
        self.assertEqual(task['status'], 'uncertain')
        self.engine.reconcile(task['key'], remote_id='verified-task-id')
        self.provider.fail_kind = None
        self.engine.dispatch()
        self.assertEqual([c[0] for c in self.provider.calls], ['contact', 'task', 'enroll'])

    def test_mode_receipts_cannot_cross_from_demo_to_live(self):
        self.engine.ingest([self.lead])
        self.provider.mode = 'live'
        with self.assertRaises(ValueError):
            Engine(self.path, self.provider)

    def test_suppression_between_claim_and_remote_call_prevents_enrollment(self):
        self.approve()
        original = self.engine.database
        injected = False

        @contextmanager
        def interleave():
            nonlocal injected
            with original() as db:
                yield db
            with original() as db:
                claiming = db.execute("SELECT 1 FROM operations WHERE kind='enroll' AND status='processing'").fetchone()
            if claiming and not injected:
                injected = True
                self.engine.database = original
                self.engine.event(self.event('lead_unsubscribed'))
                self.engine.database = interleave

        self.engine.database = interleave
        self.engine.dispatch()
        self.assertTrue(injected)
        self.assertNotIn('enroll', [call[0] for call in self.provider.calls])
        self.assertEqual(self.engine.snapshot()['leads'][0]['stage'], 'suppressed')

    def test_reconciliation_rejects_string_retry_flags(self):
        self.approve()
        self.provider.fail_kind = 'task'
        self.engine.dispatch()
        key = next(op['key'] for op in self.engine.snapshot()['operations'] if op['kind'] == 'task')
        with self.assertRaises(ValueError):
            self.engine.reconcile(key, remote_id='already-created-task', retry='false')

    def test_reply_before_dispatch_cancels_pending_enrollment(self):
        self.approve()
        self.engine.event(self.event('reply_received'))
        self.engine.dispatch()
        self.assertNotIn('enroll', [call[0] for call in self.provider.calls])
        self.assertEqual(self.engine.snapshot()['leads'][0]['stage'], 'replied')

    def test_idless_provider_events_are_deduplicated(self):
        self.approve()
        payload = self.event('reply_received')
        del payload['event_id']
        self.engine.event(payload)
        self.engine.event(payload)
        self.assertEqual(len(self.engine.snapshot()['events']), 1)


if __name__ == '__main__':
    unittest.main()
