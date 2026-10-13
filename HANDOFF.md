# Lead-to-CRM Handoff

Last updated: 2026-10-13.

Local repository: `G:/Work Stuff/lead-crm-automation`, branch `feature/lead-crm-demo`. No remote or publication. The user selected HubSpot and authorized brainstorming, planning, and implementation.

Implemented: Python standard-library intake/evidence validation, SQLite-backed approvals and operation receipts, demo/live mode separation, HubSpot contact/task adapters, Gemini supplied-source assessment, Instantly enrollment/blocklist adapters, authenticated loopback API/CLI, four inactive n8n exports, fictional fixtures, asserted offline demonstration, setup instructions, and colleague SOP.

Verification: 38 unittest checks and the offline demonstration passed on 2026-10-13. Independent review reproduced a suppression/claim race and a string retry flag causing duplicate work; both have regression tests and fixes. Review also checked server error classification after the API implementation was finalized.

Limits: no live account calls, actual n8n import/execution, public server, campaign activation, or email sending. `output/demo-run.json` is generated, ignored, and explicitly fixture-backed. Demo uses zero external calls. Instantly enrollment requires both human approval and `ALLOW_OUTREACH=true`; active campaigns can send when a lead is enrolled. Do not connect or dispatch live operations without deliberately configured test accounts.

Next account-dependent work: follow SETUP.md, create HubSpot custom properties, assign a real owner, configure model/API credentials and an Instantly test campaign, import the n8n exports, and record controlled connected runs. Do not pass simulated receipts or screenshots off as live integrations. Use separate databases for demo and live.

Current simplifications: manually supplied sources, manually verified contacts, a serialized local dispatcher, contact stages rather than deals, no frontend. Enrollment queues a CRM stage update for the next dispatch; run dispatch until pending operations are drained. Read SOP.md before reconciliation; ambiguous writes require provider inspection.
