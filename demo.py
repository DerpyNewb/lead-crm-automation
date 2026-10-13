import argparse
import json
from pathlib import Path
import tempfile

from engine import Engine, now
from providers import DemoProvider, ProviderError


class FailureDemo(DemoProvider):
    def __init__(self):
        self.fail_next_task = False

    def execute(self, kind, lead, payload):
        if kind == 'task' and self.fail_next_task:
            self.fail_next_task = False
            raise ProviderError('Simulated timeout before any external write', uncertain=True)
        return super().execute(kind, lead, payload)


def run_demo():
    root = Path(__file__).parent
    leads = json.loads((root / 'samples/leads.json').read_text())
    events = json.loads((root / 'samples/events.json').read_text())
    steps = []
    with tempfile.TemporaryDirectory() as directory:
        provider = FailureDemo()
        path = Path(directory) / 'demo.sqlite'
        engine = Engine(path, provider)
        imported = engine.ingest(leads)
        assert [r['status'] for r in imported] == ['imported', 'imported']
        duplicate = engine.ingest([leads[0]])
        rejected = engine.ingest([{**leads[0], 'email': 'invalid'}])
        assert duplicate[0]['status'] == 'duplicate' and rejected[0]['status'] == 'rejected'
        steps.append({'step': 'intake', 'imported': imported, 'duplicate': duplicate, 'invalid': rejected})

        engine.review(leads[0]['email'], 'approve', 'Demo Reviewer', '101',
                      'Fictional contact and sources checked', True, 'demo-campaign')
        delivered = engine.dispatch()
        assert [r['status'] for r in delivered] == ['done', 'done', 'done']
        delivered.extend(engine.dispatch())
        steps.append({'step': 'approved CRM contact, task, and simulated enrollment', 'operations': delivered})

        recorded, replay = engine.event(events[0]), engine.event(events[0])
        assert recorded['status'] == 'recorded' and replay['status'] == 'duplicate'
        provider.fail_next_task = True
        failed = engine.dispatch()
        assert failed[0]['status'] == 'uncertain'
        engine = Engine(path, provider)
        assert engine.dispatch() == []
        task_key = failed[0]['key']
        reconciled = engine.reconcile(task_key, retry=True)
        recovered = engine.dispatch()
        assert all(r['status'] == 'done' for r in recovered)
        steps.append({'step': 'reply replay and delivery recovery', 'event': recorded, 'replay': replay,
                      'failed_delivery': failed, 'reconciliation': reconciled,
                      'basis': 'Simulator confirms no external write occurred; safe to retry.',
                      'recovered': recovered})

        engine.review(leads[1]['email'], 'approve', 'Demo Reviewer', '101',
                      'Fictional contact checked', True, 'demo-campaign')
        suppressed = engine.event(events[1])
        engine.dispatch()
        state = engine.snapshot()
        assert [lead['stage'] for lead in state['leads']] == ['replied', 'suppressed']
        cancelled = [op for op in state['operations'] if op['status'] == 'cancelled']
        assert {op['kind'] for op in cancelled} == {'task', 'enroll'}
        assert len(state['events']) == 2
        assert not [op for op in state['operations'] if op['status'] in ('pending', 'failed', 'uncertain')]
        steps.append({'step': 'unsubscribe before enrollment', 'event': suppressed,
                      'cancelled': [op['key'] for op in cancelled]})
        return {'executed_at': now(), 'mode': 'demo', 'external_calls': 0,
                'assessment_source': 'Explicit fictional fixtures; no AI inference performed.',
                'steps': steps, 'state': state}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run the offline lead-to-CRM demonstration.')
    parser.add_argument('--output', type=Path, default=Path(__file__).parent / 'output/demo-run.json')
    args = parser.parse_args()
    report = run_demo()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('Offline demonstration passed: 2 leads, duplicate import, invalid input, approval,')
    print('CRM/task/enrollment simulation, duplicate reply, restart/reconciliation, and unsubscribe.')
    print('External API calls: 0. Assessment: fictional fixtures. Evidence:', args.output)
