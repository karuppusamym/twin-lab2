# Provider adapter experiment

> The production version is `twinlab2/providers/` (see the README's *Build sessions*): `twinlab2/providers/clarify.py` is this
> probe on the official `anthropic` SDK, and Claude sessions run the Agent SDK in `twinlab2/providers/runner.py`.
> This folder is kept as the record of the experiment.

This is an isolated package-review and clarification probe, not an enabled portal builder.
`provider_probe.py` calls Claude's Messages API through httpx. It sends only BUILD_TASK.md and
evidence.json from a checksum-matched pack, never extracts ZIP files and never executes model
output. Attachments (including images) are explicitly excluded. A checksum is an integrity
check, not an approval: production callers must first enforce the portal's frozen-pack access
and owner approval. The probe has no authorization layer and is not a web endpoint.

Run local checks from twin-lab2:

```powershell
python -m unittest discover -s tests -p test_provider_probe.py -v
```

To exercise the real API, configure ANTHROPIC_API_KEY and CLAUDE_MODEL through your environment,
then run `python experiments/provider_probe.py --live`. This sends a synthetic order example,
answers one clarification and stores the conversation under the ignored `.state/provider-probe/`.
It consumes API credits. The model is explicit; none is silently selected. No real API call
was made during development because credentials were not configured. The installed Anthropic
SDK (0.75.0) has no Managed Agents session methods; this probe uses the Messages HTTP API.
The HTTP mock tests do not establish account access, model availability or real code-building
quality. Output-token and history limits are not a monetary budget.

## Proposed production design

Use a capability-aware provider interface: prepare_package, start, read_events(cursor), reply,
interrupt, cancel, collect_outputs and reconcile_start. Report unsupported capabilities explicitly.
Keep clarification-only providers distinct from agents that can execute code. The concrete
ClarificationProvider protocol in this probe intentionally covers only start/reply. Devin,
Claude Agent SDK (isolated worker) and Copilot SDK need separate build adapters. Claude Managed
Agents is another candidate, currently documented as beta; evaluate account availability and
data handling before enabling it. A model name is configuration inside a provider, not a provider.

Relations: recording -> application evidence -> immutable pack revision -> many build sessions.
A session pins provider, model/configuration, pack hash, repository/base commit, branch/workspace,
budget and creator. Store parent_session_id for comparisons/forks. Each session has ordered
messages, provider event IDs, usage, artifact references and independent verification results.
One recording can feed several applications; sessions therefore belong to a pack/application,
not directly to one recording. Session transcripts remain in the portal's access-controlled
database; large artifacts remain in object storage. Retain native provider session references.

Replace the current UNIQUE active_app rule with request-level idempotency and per-session
workspace/branch isolation. Repeated submission of the same request returns its original session;
an explicit New session creates a new ID. Use a repository/subdirectory publication lock, rather
than preventing independent experiments. Compare PR commits and the same acceptance tests before
choosing a result. Never automatically merge competing branches.

Separate clarification, implementation, verification and publication. A natural-language answer
must not silently approve a tool action or change a frozen pack. Promote agreed scope changes into
a new reviewed pack revision and record the transition. Across providers, fork from an explicit
summary plus approved inputs and a source commit; do not promise portable native agent memory.

Use durable message outboxes: queued, sent, acknowledged, delivery_unknown, failed. Deduplicate
incoming events by provider/session/event ID and persist cursors. Do not blindly repeat a timed-out
create or reply. Serialize turns within each session. Idle/end_turn means available for further
input, not verified or published. Keep budget exhaustion, question requests and tool approvals
distinct. Costs retain provider-native units (ACUs, tokens, currency), with explicit aggregate
limits and no invented unit conversion.

Portal additions: provider/capability selector; New session; session list with pack revision,
branch, cost and state; chat with questions and delivery status; separate tool-approval controls;
Compare results. Reuse existing builds.py, build_messages, jobqueue and authorization, with a
versioned schema migration preserving existing Devin builds. No migrations or portal changes
are performed by this experiment.

Before production wiring: validate a real provider's package -> question -> reply -> committed
code -> independent CI path; reconnect after process restart; check cancellation, concurrent
sessions, duplicate requests, pagination, budget exhaustion and authorization across applications.
