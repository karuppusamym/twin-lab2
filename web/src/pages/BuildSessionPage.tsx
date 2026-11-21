import { useQuery } from "@tanstack/react-query";
import { useMemo, useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { api, data, newRequestKey, type Schemas } from "../api/client";
import { ActionButton, Badge, Crumbs, ErrorText, Notice, QueryView, useAction, useTitle } from "../components/ui";
import { amount, short, when, words } from "../lib/format";
import { useCan } from "../session";

/** One build session: its state, the conversation with the provider, what is being built. */
export function BuildSessionPage() {
  const buildId = Number(useParams().buildId);
  const build = useQuery({
    queryKey: ["build", buildId],
    queryFn: async () => data(await api.GET("/api/v1/builds/{build_id}", { params: { path: { build_id: buildId } } })),
    // the old page refreshed every 10 seconds until the session was final
    refetchInterval: (q) => (q.state.data && !q.state.data.build.final ? 10_000 : false),
  });
  useTitle(build.data ? `Session #${buildId} · ${build.data.app.name}` : undefined);
  return <QueryView query={build}>{(d) => <SessionView detail={d} />}</QueryView>;
}

function stateText(build: Schemas["BuildFull"], who: string) {
  switch (build.state) {
    case "queued": return "Waiting for the build worker.";
    case "starting": return `Starting a ${who} session.`;
    case "checking_started": return `${who} gave no clear answer to the start request, so nothing is started again until the worker has checked whether the session started.`;
    case "running": return `${who} is working.`;
    case "waiting_for_answer": return `${who} is waiting for you: see the conversation below.`;
    case "ready_for_verification": return `${who} has finished. The result is not tested yet: it is verified independently against its commit.`;
    case "failed": return "The session did not complete.";
    case "cancelled": return "The session was cancelled.";
  }
}

function SessionView({ detail }: { detail: Schemas["BuildDetail"] }) {
  const { build, app } = detail;
  const can = useCan();
  const navigate = useNavigate();
  const location = useLocation();
  const who = build.provider_label || build.provider;
  const owner = can("owner", app.id);
  const retryKey = useMemo(() => newRequestKey(), []);
  const routed = (location.state as { notice?: string | null } | null)?.notice;
  const path = { params: { path: { build_id: build.id } } };
  return (
    <>
      <Crumbs><Link to={`/apps/${app.id}`}>{app.name}</Link> · <Link to={`/apps/${app.id}/build`}>Build</Link> · Session #{build.id}</Crumbs>
      <h1>Build session #{build.id}: {app.name}, {who}{build.model && ` (${build.model})`}</h1>
      <Notice>{routed}</Notice>
      <Notice>{detail.notice}</Notice>

      <section className="nextcard" aria-label="State">
        <p id="build-state"><Badge kind={build.state}>{build.state_label}</Badge> {stateText(build, who)}</p>
        {build.error && <p className="error">{build.error}</p>}
        {build.note && <p className="muted">{build.note}</p>}
        <div className="actions">
          {!build.final && detail.capabilities.includes("interrupt") && build.provider_session_id && (
            <ActionButton secondary label="Interrupt" run={async () => data(await api.POST("/api/v1/builds/{build_id}/interrupt", path))} />
          )}
          {!build.final && <ActionButton secondary label="Cancel build" confirm="Cancel this build session?" run={async () => data(await api.POST("/api/v1/builds/{build_id}/cancel", path))} />}
          {(build.state === "failed" || build.state === "cancelled") && (
            <ActionButton label="Retry" run={async () => {
              const started = data(await api.POST("/api/v1/builds/{build_id}/retry", { ...path, body: { request_key: retryKey } }));
              navigate(`/builds/${started.build.id}`, { state: { notice: started.notice } });
            }} />
          )}
          <Link className="button secondary" to={`/apps/${app.id}/build${build.head_sha ? `?parent=${build.id}` : ""}#start`}>New session</Link>
        </div>
      </section>

      <Chat detail={detail} who={who} owner={owner} />

      <section id="details" aria-labelledby="details-h">
        <h2 id="details-h">What is being built</h2>
        <dl className="about">
          <dt>Package</dt><dd>Revision {build.package_rev && `${build.package_rev}, `}<code>{short(build.revision)}</code>, SHA-256 <code>{build.pack_sha256}</code>{" "}
            {detail.frozen && <Badge kind="verified">Frozen</Badge>}{" · "}
            <a href={`/api/v1/apps/${app.id}/briefs/${build.revision}/download`}>download</a></dd>
          <dt>Repository</dt><dd><a href={`https://github.com/${build.repo}`}>{build.repo}</a>, from <code>{build.base_branch}</code> at <code>{short(build.base_sha)}</code>
            {detail.parent && <> (the commit of <Link to={`/builds/${detail.parent.id}`}>session #{detail.parent.id}</Link>)</>}</dd>
          <dt>Branch</dt><dd><code id="branch">{build.branch}</code></dd>
          {build.previous_revision && <><dt>Previous revision</dt><dd><code>{short(build.previous_revision)}</code></dd></>}
          {build.change_summary && <><dt>Change summary</dt><dd>{build.change_summary}</dd></>}
          <dt>Budget</dt><dd>{amount(build.budget)} {build.budget_unit}{build.cost !== null && ` (${amount(build.cost)} ${build.cost_unit} used)`}</dd>
          <dt>{who} session</dt><dd>{build.provider_url
            ? <a href={build.provider_url} id="devin-link">{build.provider_session_id}</a>
            : build.provider_session_id ?? "not started yet"}
            {build.provider_status && <span className="muted"> ({build.provider_status}{build.provider_detail && `/${build.provider_detail}`})</span>}</dd>
          <dt>Pull request</dt><dd>{build.pr_number
            ? <><a href={build.pr_url ?? undefined} id="pr-link">#{build.pr_number}</a>{build.head_sha && <> head <code id="head-sha">{build.head_sha}</code></>}</>
            : build.head_sha ? <>commit <code id="head-sha">{build.head_sha}</code></> : "none yet"}</dd>
          <dt>Requested</dt><dd>by {build.requested_by} at {when(build.created_at, 19)} UTC</dd>
        </dl>
      </section>

      <section id="checks" aria-labelledby="checks-h">
        <h2 id="checks-h">Independent checks</h2>
        {build.head_sha ? (
          <>
            <p className="muted">This session is not Tested until the checks on commit <code>{short(build.head_sha)}</code> pass.{" "}
              <Link to={`/apps/${app.id}/sessions/compare?session=${build.id}`}>Compare results</Link></p>
            {build.state === "ready_for_verification" && owner && (
              <ActionButton label="Verify this build" run={async () => {
                const outcome = data(await api.POST("/api/v1/apps/{app_id}/verify-commit", {
                  params: { path: { app_id: app.id } },
                  body: { repo: build.repo, sha: build.head_sha ?? "", build_id: build.id, ref: build.branch, pr_number: build.pr_number ?? undefined },
                }));
                navigate(`/releases/${outcome.release_id}#verification`);
              }} />
            )}
          </>
        ) : <p className="muted">Checks run on the session's commit once {who} produces one.</p>}
      </section>
    </>
  );
}

function Chat({ detail, who, owner }: { detail: Schemas["BuildDetail"]; who: string; owner: boolean }) {
  const { build, app } = detail;
  const [answer, setAnswer] = useState("");
  const [scope, setScope] = useState(false);
  const [key, setKey] = useState(() => newRequestKey());
  const replyTo = [...detail.outstanding].reverse().find((q) => q.kind === "clarification")?.id ?? null;
  const send = useAction(async () => data(await api.POST("/api/v1/builds/{build_id}/messages", {
    params: { path: { build_id: build.id } },
    body: { answer, scope_change: scope, reply_to: replyTo, request_key: key },
  })), () => { setAnswer(""); setScope(false); setKey(newRequestKey()); });
  const canReply = detail.capabilities.includes("reply") && !!build.provider_session_id;
  const submit = (e: FormEvent) => { e.preventDefault(); send.mutate(undefined); };
  return (
    <section id="chat" aria-labelledby="chat-h">
      <h2 id="chat-h">Conversation</h2>
      {detail.messages.length === 0 && <p className="muted">No messages yet.</p>}
      <ol className="chat">
        {detail.messages.map((m) => (
          <li key={m.id} className={`msg ${m.direction} ${m.kind}`} id={`message-${m.id}`}>
            <strong>{m.author}</strong> <span className="muted">· {words(m.kind)} · {when(m.created_at, 19)}</span>
            {m.direction === "from_provider" && m.kind === "clarification" ? (
              <blockquote className="question">{m.body}</blockquote>
            ) : m.direction === "from_provider" && m.kind === "tool_approval" ? (
              <ToolApproval build={build} message={m} />
            ) : <p>{m.body}</p>}
            {m.direction === "to_provider" && (
              <>
                <span className={`delivery ${m.state ?? ""}`} data-delivery={m.state ?? ""}>{m.state_label}</span>
                {m.kind === "scope_change" && m.scenario_id && (
                  <span className="muted"> · proposed as a scenario for the next package revision{owner && <> (<Link to={`/apps/${app.id}/build-pack/preview`}>review</Link>)</>}</span>
                )}
              </>
            )}
            {m.error && <span className="error"> {m.error}</span>}
          </li>
        ))}
      </ol>
      {detail.outstanding.length === 0 && detail.questions.map((q) => <blockquote key={q} className="question">{q}</blockquote>)}
      {!build.final && (
        <>
          <form id="answer-form" aria-label="Answer" onSubmit={submit}>
            <label>Your answer <textarea rows={4} required value={answer} onChange={(e) => setAnswer(e.target.value)} /></label>
            <label className="check"><input type="checkbox" checked={scope} onChange={(e) => setScope(e.target.checked)} /> This changes agreed behaviour
              (not sent: it becomes a proposed scenario, reviewed as a new package revision)</label>
            <Notice>{send.data?.notice}</Notice>
            <ErrorText error={send.error} />
            <button disabled={send.isPending} title={canReply ? undefined : "The session takes replies once it has started"}>Send to {who}</button>
          </form>
          <p className="muted">A message that got no clear answer is marked Delivery uncertain and is not sent again: check the
            session{build.provider_url && <> (<a href={build.provider_url}>open it</a>)</>} before answering again.</p>
        </>
      )}
    </section>
  );
}

function ToolApproval({ build, message }: { build: Schemas["BuildFull"]; message: Schemas["BuildMessage"] }) {
  const tool = typeof message.data.tool === "string" ? message.data.tool : "a tool";
  const input = message.data.input;
  const decide = (decision: "approve" | "deny") => async () => data(await api.POST("/api/v1/builds/{build_id}/tool-decisions/{message_id}", {
    params: { path: { build_id: build.id, message_id: message.id } }, body: { decision, request_key: `tool:${message.id}:${decision}` },
  }));
  return (
    <>
      <p>Asks to run <code>{tool}</code>{input !== undefined && input !== null && <>: <code>{JSON.stringify(input)}</code></>}</p>
      {message.decision ? <p className="muted">You chose {message.decision}.</p> : !build.final && (
        <span className="actions">
          <ActionButton label="Approve" run={decide("approve")} />
          <ActionButton secondary label="Deny" run={decide("deny")} />
        </span>
      )}
    </>
  );
}
