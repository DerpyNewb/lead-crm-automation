# Lead Workflow Operating Procedure

**Audience:** The colleague assigned to intake and review. **Owner:** The business's designated CRM administrator. **Escalation:** Contact that administrator for credential, CRM mapping, or ambiguous-delivery issues. The administrator assigns actual people before using this in a business.

## 1. Prepare a batch

Use the shape in `samples/ingest.json`. Every record needs an email, company, first and last name, and one to ten source excerpts. Include a stable source ID, HTTPS source link, observation timestamp with timezone, and the actual excerpt. Do not invent a buying signal or a missing contact detail.

For demo mode, include the explicit assessment fixture. For live mode, omit the fixture; Gemini assesses the supplied excerpts. Keep private prospect files outside the repository. Sources are supplied manually; this project does not search for them.

## 2. Import and inspect

Submit through the n8n intake webhook or `python app.py request /ingest <batch.json>`. Check every returned status. `imported` means pending review, `duplicate` means the existing record was retained, and `rejected` means no lead was added. A batch can contain both successes and rejections. Inspect `/state` for failure records and correct rejected inputs before resubmitting.

## 3. Review the prospect

Open each cited source. Confirm the excerpt, source date, company match, and relevance. Separate evidence of fit from evidence of intent. An expansion announcement does not establish that the company wants to buy management services. Check the contact independently; an email passing syntax checks does not prove it exists.

Approve only a high/medium-fit prospect with checked contact details. Supply your reviewer name, a valid HubSpot owner ID, a meaningful note, and `contact_verified: true`. Include a campaign ID only when outreach enrollment is intended. Reject unsuitable records. `samples/review.json` is fictional and its owner/campaign values must not be used against real accounts.

Submit through the review webhook or `python app.py request /review <review.json>`. Expected result: an approved lead with contact/task operations and optional enrollment waiting in the durable queue. Repeating the identical decision does not add work; conflicting changes require administrator review.

## 4. Deliver pending work

Run the n8n manual dispatcher or POST `{}` to `/dispatch`. The contact is synchronized first, then its task, then any requested enrollment. A successful enrollment queues a CRM stage update for the next dispatch. Run again until no pending work remains. Check the operation receipts in `/state`. A failed contact or task prevents subsequent enrollment. Live enrollment also requires the administrator to enable `ALLOW_OUTREACH=true`.

Check the contact and task directly in HubSpot during a connected demo. Check enrollment directly in Instantly. A local `done` receipt is not proof that an email was delivered.

## 5. Handle campaign events

The authenticated event webhook accepts replies, sent events, bounces, and unsubscribes for approved leads in their selected campaigns. Run the dispatcher after events. A reply adds an owned follow-up task and cancels any pending enrollment. A bounce or unsubscribe suppresses locally immediately, cancels unsent work, and queues Instantly workspace blocklisting.

Check blocklisting delivery status. If provider access fails, pause sending through the provider's own controls until the administrator resolves it. Local suppression cannot retroactively stop a message already sent or replace remote suppression while the integration is unavailable.

Unknown prospects, unmatched campaigns, bad timestamps, and conflicting event IDs require administrator review. Do not automatically approve an unknown lead to make an event pass.

## 6. Recover failures

For `failed`, correct the cause before dispatching again: missing credentials, owner/property mapping, disabled outreach, or provider rejection. The next dispatch retries known failed operations.

For `uncertain`, do not retry blindly. Check the provider for the contact, task, enrollment, or blocklist entry:

- If it exists, POST `{"key":"<operation key>","remote_id":"<verified provider ID>"}` to `/reconcile`.
- If the administrator confirms no write occurred, POST `{"key":"<operation key>","retry":true}` to `/reconcile`.

Then run the dispatcher and verify the result. Keep the review note and evidence of the provider check. Do not clear or recreate the database as a recovery shortcut; that destroys duplicate-prevention receipts.

## 7. Preserve evidence

Keep the operational database private and back it up before changing versions. Publish fictional samples and sanitized recordings only. Describe demo receipts as simulations, provider contract tests as tests, and connected runs as connected runs. Never claim production use or time savings without measured evidence.
