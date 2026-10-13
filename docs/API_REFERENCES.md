# Provider contracts

Checked against primary documentation on 2026-10-13. These are HTTP contract checks, not connected-account verification.

| Provider | Contract | Source |
| --- | --- | --- |
| HubSpot | GET contact by escaped email with `idProperty=email`; PATCH existing ID or POST after 404. Omit lifecycle stage. | [Contact guide](https://developers.hubspot.com/docs/api-reference/legacy/crm/objects/contacts/guide) |
| HubSpot | POST `/crm/v3/objects/tasks`; `hs_timestamp`, subject, body, owner; task-to-contact association type 204. | [Task guide](https://developers.hubspot.com/docs/api-reference/legacy/crm/activities/tasks/guide) |
| Instantly | POST `/api/v2/leads`; campaign field is `campaign`; `skip_if_in_campaign: true`; bearer authentication. | [Create lead](https://developer.instantly.ai/api-reference/lead/create-lead) |
| Instantly | POST `/api/v2/block-lists-entries` with `bl_value` containing email. No enrollment ID required. | [Create block list entry](https://developer.instantly.ai/api-reference/blocklistentry/create-block-list-entry) |
| Instantly | Webhook creation accepts an optional `headers` object for delivery authentication. | [Create webhook](https://developer.instantly.ai/api-reference/webhook/create-webhook) |
| Gemini | POST model `:generateContent` with JSON MIME type and JSON schema; API key in `x-goog-api-key`. Revalidate enums and supplied-source quotes locally. | [Generate content](https://ai.google.dev/api/generate-content), [Structured output](https://ai.google.dev/gemini-api/docs/structured-output) |

Create HubSpot contact properties `automation_stage`, `automation_fit`, `automation_intent`, `automation_summary`, and `automation_evidence` as strings before live dispatch. Evidence is JSON serialized into a string; each item includes its source ID, verbatim quote, source URL, and observation date for review directly from HubSpot. Owner IDs must exist in the connected account. An Instantly lead receipt confirms enrollment, not delivery; this adapter never activates a campaign.

Instantly's workspace blocklist prevents contact even when a lead already exists in an ongoing campaign. Its Google Sheets blocklist is checked only at import, so existing-campaign suppression requires the workspace Blocklist tab represented by this API. [Official global blocklist guidance](https://help.instantly.ai/en/articles/6192983-global-blocklist).

Live credentials are read from environment variables. `ALLOW_OUTREACH=true` enables enrollment only; safety blocklisting remains allowed. The caller owns approvals, state, delivery ordering, and reconciliation. Adapters never retry. Unknown transport failures, server errors, and malformed successful write responses carry `ProviderError.uncertain=True`; known 4xx responses are definite failures. Error messages exclude request bodies, URLs, tokens, and provider response bodies.

TDD evidence: initial stub run produced 13 assertion failures across 10 contract tests with no errors. Implementing the adapters made all 10 pass. A loopback server transport check was then added to exercise real urllib JSON and HTTP-status handling. No external writes were executed.

Evidence-link regression: the contact contract assertion failed because serialized evidence omitted `url` and `observed_at`; adding these from the submitted source lookup restored all 11 provider tests.
