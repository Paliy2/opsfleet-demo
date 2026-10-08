# Extending the agent with capabilities

A **capability** is a plug-in that gives the orchestrator new tools plus a short prompt hint
describing them. The orchestrator core (analysis, schema, reports, preferences, deletion) stays
fixed; everything optional (`charts`, `report_library`, `email_report`) is a capability that ops
can switch on or off with one env var.

## The seam

`src/opsfleet_agent/agents/capability.py`

```python
class Capability(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def prompt_hint(self) -> str: ...
    def tools(self, turn: TurnContext) -> list[BaseTool]: ...
```

- `TurnContext` is the per-turn state a tool may use: `scope` (user id, role, brands),
  `session_id`, `progress` (emit `ProgressEvent`s), `preferences`, and the turn bookkeeping
  lists (`result_ids`, `report_ids`, `chart_ids`, `chart_paths`, ...).
- `Orchestrator(capabilities=...)` appends every capability's tools to the core tools each turn
  and fails fast if two tools share a name (a capability cannot shadow `delete_reports`).
- `prompt_hint` lines are composed by `compose_system(..., hints=...)` into a `## More tools`
  section **after** the editable prompts (Langfuse) and **before** the code-owned safety rules,
  scope and date. A hint can never override the safety rules.
- `Settings.capabilities` (`OPSFLEET_CAPABILITIES=charts,report_library,email_report`) selects
  which capabilities are built in `wiring.build_capabilities`. Unknown or repeated names fail at
  startup. An empty value disables all optional capabilities.

## Adding a capability: worked example `email_report`

1. **Port** — the external system behind a Protocol.
   `ports/mail.py`: `ReportMailer.send(mail: OutgoingMail) -> MailReceipt`
   (entities in `entities/mail.py`).
2. **Interactor** — business rules, imports only ports and entities, TDD.
   `interactors/email_report.py`: `EmailReport(get_report, mailer, address_of)` reuses the
   owner-scoped `GetReport` (a non-owner gets `NotFoundError`), allows only the requester's own
   address (`RecipientDeniedError`, the exfiltration guard), and runs `ensure_no_pii` on the body.
3. **Gateway** — the adapter, the only place that touches the SDK or network.
   `gateways/mail/outbox.py` (`OutboxMailer`, writes `.eml` files to `mail_outbox_dir`, the safe
   default) and `gateways/mail/smtp.py` (`SmtpMailer`, `smtplib` via `anyio.to_thread`, maps
   failures to `MailDeliveryError`).
4. **Typed errors** — add codes to `core/errors.py` (`E_RECIPIENT_DENIED`, `E_MAIL_FAILED`).
5. **Capability** — `agents/capabilities/email.py`: `EmailReportCapability` with `name`,
   `prompt_hint` and a thin tool that calls the interactor, catches only the typed errors and
   returns a status the model can act on (`SENT`, `NOT_FOUND`, `RECIPIENT_DENIED`, `BLOCKED`,
   `FAILED`). It emits a `ProgressEvent(stage="email")`, which the telemetry sink already counts in
   `opsfleet_agent_events{stage="email"}`; failures carry a `code` and land in
   `opsfleet_tool_errors{code=...}`.
6. **Wiring** — add `CapabilityName.EMAIL_REPORT` in `core/config.py` and one factory entry in
   `wiring.build_capabilities`. Gateway selection lives next to it (`build_mailer`, chosen by
   `OPSFLEET_MAILER=outbox|smtp`).
7. **Settings toggle** — new settings go in `Settings` with safe defaults and startup validation
   (`mail_outbox_dir`, `mail_from`, `user_email_template`, `smtp_*`; `mailer=smtp` requires
   `smtp_host`).
8. **Tests** — `tests/unit/test_email_report.py` (interactor rules and gateways with fakes) and
   `tests/unit/test_capabilities.py` (tool only bound when enabled, hint placement, refusal text).
   Every security rule gets a red-team style test.
9. **Evals** — a capability case (`report-email-self` in `evals/datasets/capability.jsonl`) and a
   red-team case (`exfiltration-email-external` in `evals/datasets/redteam.jsonl`).
10. **UI labels** — optional: add the progress stage label in `cli/main.py` and `ui/app.py`.

## Design sketch: `web_trends`

- Port `TrendSearch.search(query: str, limit: int) -> tuple[TrendItem, ...]` in `ports/`.
- Interactor `SearchTrends` enforces a query length cap, strips user identifiers from the query
  (no internal data leaves the company) and caps results.
- Gateway wraps a search API with `httpx`, timeouts and a typed `E_SEARCH_FAILED` error.
- `WebTrendsCapability` exposes `web_trends(query)` and a hint stating results are external,
  unverified context that must be cited and never mixed with company figures.
- Add `CapabilityName.WEB_TRENDS`, a factory entry, an API key `SecretStr` setting, unit tests
  with a fake `TrendSearch`, and an injection red-team case (search results are untrusted input).
