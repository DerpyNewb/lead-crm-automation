# Setup

## Offline engine

Python 3.11+ is sufficient. Run from this repository:

```powershell
python -m unittest discover -v
python demo.py
$env:ENGINE_API_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
python app.py serve --mode demo
```

The server binds to loopback port 8765. In a second terminal, set the same `ENGINE_API_TOKEN`, then run:

```powershell
python app.py request /ingest samples/ingest.json
python app.py request /review samples/review.json
```

For dispatch, create a JSON file containing `{}` and run `python app.py request /dispatch <file.json>`. `python app.py state` prints state. The standalone `demo.py` uses its own temporary database, so it does not modify the server's state. Default server databases are `output/demo.sqlite` and `output/live.sqlite`; choose `--db` to use another private path. Ports can be changed with `--port`; use the same `--port` on server, request, and state commands.

## n8n

1. Run n8n on the same host as the engine for the local demonstration. In Docker, `127.0.0.1` refers to the container; the exported local URLs require a same-host runtime and are not a ready Docker deployment.
2. Import `n8n-intake.json`, `n8n-review.json`, `n8n-events.json`, and `n8n-dispatch.json`. Exports are inactive and contain no credentials.
3. Create a Header Auth credential for incoming webhooks: header name `X-Workflow-Token`, value a separate generated shared secret. Attach it to the three Webhook nodes.
4. Create a Header Auth credential for outgoing engine calls: header name `Authorization`, value `Bearer <ENGINE_API_TOKEN>`. Attach it to each HTTP Request node. Bind credentials after import; never embed secrets into the JSON exports.
5. Start each webhook in test mode and POST its corresponding JSON input with `X-Workflow-Token`. Use `samples/ingest.json` and `samples/review.json`; submit individual objects from `samples/events.json` for events.
6. Execute the manual dispatcher workflow after reviews and events. Inspect engine responses and receipts. Record actual import and execution results separately from the included structural checks.

For a connected event demo, provide a reachable HTTPS n8n webhook in an environment that can privately reach the engine. Instantly webhook configuration supports a custom `headers` object; configure `X-Workflow-Token` with the inbound secret. Do not publish the loopback Python server or disable webhook authentication. Public hosting and private networking are deployment work outside this local build.

## HubSpot

Use a dedicated test account and its documented private-app or OAuth access token. Enable contact read/write access and the account's required task permissions. Read the current [contact](https://developers.hubspot.com/docs/api-reference/legacy/crm/objects/contacts/guide) and [task](https://developers.hubspot.com/docs/api-reference/legacy/crm/activities/tasks/guide) scope requirements when creating the token.

Create these contact properties as custom strings in the chosen property group:

| Internal name | Suggested field type | Purpose |
| --- | --- | --- |
| `automation_stage` | Single-line text | Approved, enrolled, replied, or suppressed |
| `automation_fit` | Single-line text | High, medium, low, or unknown |
| `automation_intent` | Single-line text | Evidence classification |
| `automation_summary` | Multi-line text | Assessment summary |
| `automation_evidence` | Multi-line text | Serialized evidence with source references |

Assign an existing numeric owner ID when approving. The adapter updates contact details and the custom properties, preserving default lifecycle stage. It creates tasks associated with the contact using task-to-contact association type 204. The local database remains the approval, failure, and event audit.

## Gemini and Instantly

Set environment variables using `.env.example` as a list. The app does not automatically load `.env`; use your shell or a secret manager. Live input sources go to Gemini for assessment; only use information suitable for the configured provider account.

- `GEMINI_API_KEY`: Gemini API access.
- `GEMINI_MODEL`: a supported model name available to your account, without the `models/` prefix. Model availability is intentionally not hard-coded.
- `HUBSPOT_ACCESS_TOKEN`: the selected test account's token.
- `INSTANTLY_API_KEY`: API v2 access for lead creation and blocklist entry creation.
- `ALLOW_OUTREACH`: `false` by default. Only `true` enables enrollment; the adapter never activates a campaign. Enrollment into an already active campaign may cause sending.

Start with `python app.py serve --mode live` using a separate database. Omit the sample assessment fixture from live intake. Replace fictional sources, the demo owner ID, and demo campaign ID with deliberately chosen test values. Do not send to reserved `.example` addresses; use only controlled test inboxes for connected checks.

For real sending, configure and review the campaign itself, including its stop-on-reply behavior and unsubscribe handling. The application handles four event types; configure Instantly subscriptions for `reply_received`, `email_sent`, `email_bounced`, and `lead_unsubscribed`.

## Connected verification checklist

- [ ] Gemini returns a locally accepted source-backed assessment.
- [ ] An approved test contact and associated owned task appear in HubSpot.
- [ ] Repeated dispatch and identical review do not create another task or enrollment.
- [ ] Intended test enrollment appears in Instantly.
- [ ] Authenticated real campaign events update state and create only one task on replay.
- [ ] Bounce/unsubscribe cancels pending enrollment and remote blocklisting is confirmed.
- [ ] A deliberately interrupted operation is reconciled before retry.

These are account-dependent checks, not claims about execution already completed. See [provider contract sources](docs/API_REFERENCES.md) for exact API shapes.
