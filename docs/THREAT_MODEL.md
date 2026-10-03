# Threat model

Assets: customer PII, pricing/margins, money movement, external commitments, credentials.

| Threat | Control | Evidence |
|---|---|---|
| Prompt injection in documents, emails, web pages, messages | Agent has no shell/file/browser/web/code tools; only typed tools; tools enforce role + approval server-side; document text returned inside `<untrusted_document>` envelopes; injection phrases flagged; SOUL instructs to treat as data | `test_malicious_document_is_contained`, Hermes toolset listing, `test_tools_drive_workflow_and_agent_cannot_self_approve` |
| Agent self-approval / model "yes" treated as consent | Agent role lacks `decide:approval`; decisions only via dashboard session, local CLI, or MCP elicitation answered through Hermes' human prompt (off by default) | `test_core_workflow`, `test_owner_decision_uses_elicitation_and_payload_hash` |
| Approval replay, expiry bypass, changed payload | Hash-bound, expiring, single-use (`approved→executing` atomic), re-derived at decide and execute | `test_expired_and_replayed_approvals_cannot_execute`, `test_changed_destination_invalidates_message_approval`, `test_dashboard_approval_requires_presented_hash` |
| Cross-job / cross-role data access | Every service call takes an `Actor`; job scoping by assignment; uniform "no access" (no existence probing); financial fields stripped for field roles | `test_foreman_cross_job_access_denied`, `test_foreman_sees_only_assigned_job_without_money` |
| Stolen/forged MCP token | Random 256-bit tokens, SHA-256 at rest, expiry, revocation, ASGI guard rejects before MCP parsing; MCP listener internal-only | `test_unauthenticated_requests_rejected` |
| Dashboard session attacks | DB-backed sessions (hashed ids), HttpOnly + SameSite=Strict + Secure cookie, CSRF on every POST, login rate limits, strict CSP, no-store | `test_web.py` |
| Malicious uploads | Size cap, magic-byte typing, archives/HTML/executables refused, active PDFs quarantined, content-addressed paths, sanitized names, downloads with sandbox CSP | `test_malicious_document_is_contained` |
| SSRF / data exfiltration via fetch | Research fetch only https to official-domain allowlist, no query strings/credentials/ports, no redirects; outbound adapters allowlisted hosts | `test_official_source_fetch_is_allowlisted` |
| Duplicate or blind resends | Idempotency keys, provider event-ID dedupe, UNKNOWN on ambiguous timeouts, lease recovery | `test_durability.py` |
| SQL injection | SQLAlchemy parameterized queries only | code review |
| Secret disclosure | Secrets only in env files (600); never returned by tools; export excludes hashes/sessions; errors to the agent are generic | `mcp_server._run`, `export.py` |
| Runaway spend | `agent.max_turns: 40`, MCP timeouts, routines without model by default, host usage guard (token cap → stop Hermes + kill switch) | `deploy/scripts/usage-guard.sh` |
| Server compromise / admin | **Not defended**: a root user or DB superuser can alter records and audit rows. Audit is append-only for the app role only. Backups are encrypted; keep the key off-server. | documented |
| Shared VPS neighbors | Own compose project, internal network for DB/MCP, no Docker socket, no host mounts for Hermes, `no-new-privileges`, dropped capabilities, read-only root FS for app containers, resource limits | `deploy/docker-compose.yml` |
