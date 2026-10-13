from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit


def now():
    return datetime.now(timezone.utc).isoformat()


def text(value, field, limit=500):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{field} must be nonempty text of at most {limit} characters')
    return value.strip()


def email_address(value):
    value = text(value, 'email', 254).lower()
    if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', value):
        raise ValueError('email has invalid syntax')
    return value


def timestamp(value):
    value = text(value, 'timestamp', 50)
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('timestamp must be ISO 8601') from None
    if result.tzinfo is None or 'T' not in value or result > datetime.now(timezone.utc):
        raise ValueError('timestamp must include a timezone and not be in the future')
    return result.isoformat()


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def normalize(record):
    if not isinstance(record, dict):
        raise ValueError('lead must be an object')
    lead = {k: text(record.get(k), k, 200) for k in ('company', 'first_name', 'last_name')}
    lead['email'] = email_address(record.get('email'))
    sources = record.get('sources')
    if not isinstance(sources, list) or not 1 <= len(sources) <= 10:
        raise ValueError('sources must contain one to ten source excerpts')
    lead['sources'] = []
    ids = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError('source must be an object')
        source_id = text(source.get('id'), 'source id', 100)
        if source_id in ids:
            raise ValueError('source ids must be unique')
        ids.add(source_id)
        url = text(source.get('url'), 'source URL', 2000)
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('source URL must be HTTPS without embedded credentials')
        if parsed.hostname in ('localhost',) or parsed.hostname.endswith('.localhost'):
            raise ValueError('source URL must identify a public source')
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address and not address.is_global:
            raise ValueError('source URL must identify a public source')
        lead['sources'].append({'id': source_id, 'url': url,
                                'observed_at': timestamp(source.get('observed_at')),
                                'text': text(source.get('text'), 'source text', 12000)})
    if 'assessment' in record:
        lead['assessment'] = record['assessment']
    return lead


def validate_assessment(value, sources):
    if not isinstance(value, dict):
        raise ValueError('assessment must be an object')
    if value.get('fit') not in ('high', 'medium', 'low', 'unknown'):
        raise ValueError('assessment fit is invalid')
    if value.get('intent') not in ('explicit_request', 'expansion', 'none', 'unknown'):
        raise ValueError('assessment intent is invalid')
    summary = text(value.get('summary'), 'assessment summary', 2000)
    evidence = value.get('evidence')
    if not isinstance(evidence, list) or len(evidence) > 20:
        raise ValueError('evidence must be a list of at most twenty excerpts')
    if not evidence and (value['fit'] != 'unknown' or value['intent'] not in ('none', 'unknown')):
        raise ValueError('substantive classifications require evidence')
    by_id = {s['id']: s for s in sources}
    clean = []
    for item in evidence:
        if not isinstance(item, dict) or item.get('source_id') not in by_id:
            raise ValueError('evidence references an unknown source')
        quote = text(item.get('quote'), 'evidence quote', 2000)
        if quote not in by_id[item['source_id']]['text']:
            raise ValueError('evidence quote is absent from the source')
        clean.append({'source_id': item['source_id'], 'quote': quote})
    return {'fit': value['fit'], 'intent': value['intent'], 'summary': summary, 'evidence': clean}


class Engine:
    def __init__(self, db_path, provider):
        self.path, self.provider = Path(db_path), provider
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.database() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata (mode TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS leads (email TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operations (
                    key TEXT PRIMARY KEY, email TEXT NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    result TEXT, attempts INTEGER NOT NULL DEFAULT 0, error TEXT);
                CREATE TABLE IF NOT EXISTS events (key TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS failures (
                    id INTEGER PRIMARY KEY, stage TEXT, message TEXT, created_at TEXT);
            ''')
            mode = db.execute('SELECT mode FROM metadata').fetchone()
            if mode and mode['mode'] != provider.mode:
                raise ValueError('database mode differs from provider; use a separate database')
            if not mode:
                db.execute('INSERT INTO metadata VALUES (?)', (provider.mode,))
            db.execute("UPDATE operations SET status='uncertain', error='Interrupted delivery; reconcile before retry' WHERE status='processing'")

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def failure(self, stage, message):
        with self.database() as db:
            db.execute('INSERT INTO failures(stage,message,created_at) VALUES(?,?,?)',
                       (stage, message, now()))

    def load(self, db, email):
        row = db.execute('SELECT data FROM leads WHERE email=?', (email,)).fetchone()
        if not row:
            raise ValueError('lead does not exist')
        return json.loads(row['data'])

    def save(self, db, lead):
        db.execute('UPDATE leads SET data=? WHERE email=?', (encode(lead), lead['email']))

    def queue(self, db, key, lead, kind, payload=None):
        db.execute('INSERT OR IGNORE INTO operations(key,email,kind,payload) VALUES(?,?,?,?)',
                   (key, lead['email'], kind, encode(payload or {})))

    def ingest(self, records):
        if not isinstance(records, list) or not 1 <= len(records) <= 100:
            raise ValueError('records must contain one to one hundred leads')
        results = []
        for record in records:
            try:
                lead = normalize(record)
                with self.database() as db:
                    if db.execute('SELECT 1 FROM leads WHERE email=?', (lead['email'],)).fetchone():
                        results.append({'email': lead['email'], 'status': 'duplicate'})
                        continue
                assessment = self.provider.assess(lead)
                lead.update(assessment=validate_assessment(assessment, lead['sources']),
                            stage='pending_review', contact_validity='syntax_only',
                            created_at=now(), history=[])
                with self.database() as db:
                    result = db.execute('INSERT OR IGNORE INTO leads VALUES(?,?)',
                                        (lead['email'], encode(lead)))
                    status = 'imported' if result.rowcount else 'duplicate'
                results.append({'email': lead['email'], 'status': status})
            except Exception as exc:
                message = str(exc) if isinstance(exc, ValueError) else 'Assessment provider failed; check configuration or provider availability'
                self.failure('ingest', message)
                results.append({'status': 'rejected', 'error': message})
        return results

    def review(self, email, decision, reviewer, owner_id, note,
               contact_verified=False, campaign_id=None):
        email = email_address(email)
        reviewer, note = text(reviewer, 'reviewer', 200), text(note, 'review note', 2000)
        if decision not in ('approve', 'reject'):
            raise ValueError('decision must be approve or reject')
        if decision == 'approve':
            owner_id = text(owner_id, 'owner ID', 100)
            if not owner_id.isdigit() or contact_verified is not True:
                raise ValueError('approval requires a numeric owner ID and contact_verified true')
            if campaign_id is not None:
                campaign_id = text(campaign_id, 'campaign ID', 100)
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            lead = self.load(db, email)
            requested = {'reviewer': reviewer, 'owner_id': owner_id, 'note': note,
                         'contact_verified': contact_verified, 'campaign_id': campaign_id,
                         'decision': decision}
            if lead['stage'] != 'pending_review':
                existing = lead.get('approval', {})
                if all(existing.get(k) == v for k, v in requested.items()):
                    return lead
                raise ValueError('lead already reviewed; conflicting decisions require manual review')
            if decision == 'approve' and lead['assessment']['fit'] not in ('high', 'medium'):
                raise ValueError('only high or medium fit leads can be approved')
            lead['approval'] = {**requested, 'reviewed_at': now()}
            lead['stage'] = 'approved' if decision == 'approve' else 'rejected'
            if decision == 'approve':
                lead['contact_validity'] = 'reviewer_verified'
                lead['campaign_id'] = campaign_id
                self.queue(db, f'contact:{email}', lead, 'contact')
                self.queue(db, f'initial-task:{email}', lead, 'task', {
                    'subject': 'Review approved property management prospect',
                    'body': note, 'due_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()})
                if campaign_id:
                    self.queue(db, f'enroll:{email}', lead, 'enroll')
            self.save(db, lead)
            return lead

    def event(self, payload):
        try:
            if not isinstance(payload, dict):
                raise ValueError('event must be an object')
            kind = payload.get('event_type')
            if kind not in ('reply_received', 'email_bounced', 'lead_unsubscribed', 'email_sent'):
                raise ValueError('unsupported event type')
            data = {'event_type': kind, 'lead_email': email_address(payload.get('lead_email')),
                    'campaign_id': text(payload.get('campaign_id'), 'campaign ID', 100),
                    'workspace': text(payload.get('workspace'), 'workspace', 100),
                    'timestamp': timestamp(payload.get('timestamp')),
                    'email_id': payload.get('email_id', ''),
                    'reply_text': payload.get('reply_text') or payload.get('reply_text_snippet', '')}
            for field in ('email_id', 'reply_text'):
                if not isinstance(data[field], str) or len(data[field]) > 12000:
                    raise ValueError(f'{field} must be bounded text')
            digest = hashlib.sha256(encode(data).encode()).hexdigest()
            event_id = payload.get('event_id')
            key = (data['workspace'] + ':' + text(event_id, 'event ID', 200)) if event_id else digest
            with self.database() as db:
                db.execute('BEGIN IMMEDIATE')
                lead = self.load(db, data['lead_email'])
                if lead.get('campaign_id') != data['campaign_id'] or not lead.get('approval'):
                    raise ValueError('event campaign does not match an approved lead')
                receipt = db.execute('SELECT data FROM events WHERE key=?', (key,)).fetchone()
                if receipt:
                    if receipt['data'] != encode(data):
                        raise ValueError('event ID conflicts with an existing receipt')
                    return {'status': 'duplicate', 'key': key}
                db.execute('INSERT INTO events VALUES(?,?)', (key, encode(data)))
                lead['history'].append({**data, 'received_at': now(), 'key': key})
                if kind in ('email_bounced', 'lead_unsubscribed'):
                    lead['stage'], lead['suppression_reason'] = 'suppressed', kind
                    db.execute("UPDATE operations SET status='cancelled' WHERE email=? AND kind IN ('enroll','task') AND status IN ('pending','failed')", (lead['email'],))
                    self.queue(db, f'suppress:{lead["email"]}', lead, 'suppress')
                elif lead['stage'] != 'suppressed' and kind == 'reply_received':
                    lead['stage'] = 'replied'
                    db.execute("UPDATE operations SET status='cancelled' WHERE email=? AND kind='enroll' AND status IN ('pending','failed')", (lead['email'],))
                    self.queue(db, f'reply-task:{key}', lead, 'task', {
                        'subject': 'Review prospect reply', 'body': data['reply_text'] or 'Open the reply in Instantly.',
                        'due_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()})
                self.queue(db, f'event-contact:{key}', lead, 'contact')
                self.save(db, lead)
                return {'status': 'recorded', 'key': key}
        except ValueError as exc:
            self.failure('event', str(exc))
            raise

    def finish(self, db, operation, result):
        if not isinstance(result, dict) or not isinstance(result.get('id'), str) or not result['id']:
            raise RuntimeError('provider success lacks a remote identifier')
        lead = self.load(db, operation['email'])
        if operation['kind'] == 'contact':
            lead['contact_id'] = result['id']
        if operation['kind'] == 'enroll':
            lead['enrollment_id'] = result['id']
            if lead['stage'] == 'approved':
                lead['stage'] = 'enrolled'
            self.queue(db, f'enrollment-contact:{lead["email"]}', lead, 'contact')
        db.execute("UPDATE operations SET status='done',result=?,error=NULL WHERE key=?", (encode(result), operation['key']))
        self.save(db, lead)

    def dispatch(self):
        outcomes = []
        with self.database() as db:
            candidates = db.execute("SELECT key FROM operations WHERE status IN ('pending','failed') ORDER BY CASE WHEN kind='suppress' THEN 0 ELSE 1 END, rowid").fetchall()
        for candidate in candidates:
            with self.database() as db:
                db.execute('BEGIN IMMEDIATE')
                operation = db.execute('SELECT rowid,* FROM operations WHERE key=?', (candidate['key'],)).fetchone()
                if operation['status'] not in ('pending', 'failed'):
                    continue
                lead = self.load(db, operation['email'])
                if lead['stage'] == 'suppressed' and operation['kind'] in ('enroll', 'task'):
                    db.execute("UPDATE operations SET status='cancelled' WHERE key=?", (operation['key'],))
                    continue
                if operation['kind'] != 'suppress':
                    blocked = db.execute("SELECT 1 FROM operations WHERE email=? AND rowid<? AND status IN ('pending','failed','processing','uncertain')", (operation['email'], operation['rowid'])).fetchone()
                    if blocked:
                        continue
                db.execute("UPDATE operations SET status='processing',attempts=attempts+1 WHERE key=?", (operation['key'],))
            # ponytail: one SQLite writer during provider calls; leased jobs before multi-worker throughput.
            with self.database() as db:
                db.execute('BEGIN IMMEDIATE')
                lead = self.load(db, operation['email'])
                current = db.execute('SELECT status FROM operations WHERE key=?', (operation['key'],)).fetchone()
                if current['status'] != 'processing':
                    continue
                if (operation['kind'] == 'enroll' and lead['stage'] != 'approved') or (operation['kind'] == 'task' and lead['stage'] == 'suppressed'):
                    db.execute("UPDATE operations SET status='cancelled' WHERE key=?", (operation['key'],))
                    continue
                try:
                    result = self.provider.execute(operation['kind'], lead, json.loads(operation['payload']))
                    self.finish(db, operation, result)
                    outcomes.append({'key': operation['key'], 'status': 'done'})
                except Exception as exc:
                    uncertain = getattr(exc, 'uncertain', True)
                    status = 'uncertain' if uncertain else 'failed'
                    message = 'Provider delivery uncertain; reconcile before retry' if uncertain else 'Provider rejected operation; correct configuration or data before retry'
                    db.execute('UPDATE operations SET status=?,error=? WHERE key=?', (status, message, operation['key']))
                    db.execute('INSERT INTO failures(stage,message,created_at) VALUES(?,?,?)', (operation['kind'], message, now()))
                    outcomes.append({'key': operation['key'], 'status': status})
        return outcomes

    def reconcile(self, key, remote_id=None, retry=False):
        key = text(key, 'operation key', 500)
        if not isinstance(retry, bool):
            raise ValueError('retry must be a boolean')
        if (remote_id is None) == (retry is not True):
            raise ValueError('supply either a verified remote ID or retry true after confirming no remote write')
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            operation = db.execute('SELECT * FROM operations WHERE key=?', (key,)).fetchone()
            if not operation or operation['status'] not in ('uncertain', 'failed'):
                raise ValueError('operation is not awaiting reconciliation')
            if retry:
                db.execute("UPDATE operations SET status='pending',error=NULL WHERE key=?", (key,))
            else:
                self.finish(db, operation, {'id': text(remote_id, 'remote ID', 200), 'reconciled_at': now()})
            return {'key': key, 'status': 'pending' if retry else 'done'}

    def snapshot(self):
        with self.database() as db:
            operations = [dict(row) for row in db.execute('SELECT * FROM operations ORDER BY rowid')]
            for operation in operations:
                operation['payload'] = json.loads(operation['payload'])
                operation['result'] = json.loads(operation['result']) if operation['result'] else None
            return {'mode': self.provider.mode,
                    'leads': [json.loads(row['data']) for row in db.execute('SELECT data FROM leads ORDER BY email')],
                    'operations': operations,
                    'events': [json.loads(row['data']) for row in db.execute('SELECT data FROM events ORDER BY rowid')],
                    'failures': [dict(row) for row in db.execute('SELECT * FROM failures ORDER BY id')]}
