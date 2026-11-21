// Scope triage: one card per application a recording found and nobody decided on yet, with what
// it is (identity, surface, steps, sample screens and controls) and what it looks like (ranked
// suggestions), and explicit decisions: Replicate, Same as … (a merge, previewed first), or Not
// replicated (used only). Decisions apply to one card or to every selected card, and can be
// reversed. "Needs a scope decision" is never offered as a choice.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, data, type Schemas } from "../api/client";
import { Combobox } from "../components/Combobox";
import { SurfaceBadge } from "../components/Workboard";
import { Badge, Crumbs, ErrorText, Notice, QueryView, useAction, useTitle } from "../components/ui";
import { plural } from "../lib/format";
import type { Decision } from "../lib/scope";

const CONFIRM: Record<Decision, (names: string) => string> = {
  replicate: (names) => `Replicate ${names}? ${names} then follow the journey to their own builder package. You can reverse this later.`,
  not_replicated: (names) => `Record ${names} as not replicated? Later recordings note them only as used. You can reverse this later.`,
};

export function TriagePage() {
  const recordingId = useParams().recordingId;
  const [params, setParams] = useSearchParams();
  const all = params.get("status") === "all";
  const triage = useQuery({
    queryKey: ["triage", recordingId ?? "", all],
    queryFn: async () => data(await api.GET("/api/v1/triage", {
      params: { query: { recording_id: recordingId, status: all ? "all" : undefined } },
    })),
  });
  useTitle("Scope triage");
  return (
    <>
      <Crumbs>{recordingId
        ? <><Link to={`/recordings/${encodeURIComponent(recordingId)}`}>{triage.data?.recording?.name ?? "Recording"}</Link> · Scope triage</>
        : <><Link to="/">Home</Link> · Scope triage</>}</Crumbs>
      <h1>Scope triage</h1>
      <p>For each application a recording found, decide what happens to it: <b>Replicate</b> it (it gets its own builder package),
        say it is the <b>same as</b> an application you have (its steps move there), or record it as <b>not replicated</b> (later recordings note it only as used).
        Every decision can be reversed.</p>
      <label className="inline-check"><input type="checkbox" checked={all} onChange={(e) => setParams(e.target.checked ? { status: "all" } : {})} /> Show decided applications too</label>
      <QueryView query={triage}>{(t) => <TriageView triage={t} />}</QueryView>
    </>
  );
}

function TriageView({ triage }: { triage: Schemas["Triage"] }) {
  const [chosen, setChosen] = useState<number[]>([]);
  const [message, setMessage] = useState("");
  const decide = useAction(
    async (body: Schemas["TriageDecisions"]) => data(await api.POST("/api/v1/triage/decisions", { body })),
    (outcome) => { setMessage(outcome.message); setChosen([]); },
    [["triage"], ["apps"], ["app"], ["recording"], ["home"]],
  );
  const byId = new Map(triage.apps.map((c) => [c.app_id, c]));
  const run = (ids: number[], decision: Decision) => {
    const names = ids.map((id) => byId.get(id)?.name ?? String(id)).join(", ");
    if (!window.confirm(CONFIRM[decision](names))) return;
    decide.mutate({ decisions: ids.map((id) => ({ app_id: id, decision, expected_revision: byId.get(id)?.revision })) });
  };
  if (!triage.apps.length) {
    return (
      <>
        <Notice>{message}</Notice>
        <p>No application waits for a scope decision.{triage.recording && <> Back to <Link to={`/recordings/${encodeURIComponent(triage.recording.id)}`}>the workboard</Link>.</>}</p>
      </>
    );
  }
  const toggle = (id: number) => setChosen((c) => (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]));
  return (
    <>
      <Notice>{message}</Notice>
      <ErrorText error={decide.error} />
      <div className="toolbar" role="group" aria-label="Decide for the selected applications">
        <label className="inline-check"><input type="checkbox" checked={chosen.length === triage.apps.length}
          onChange={(e) => setChosen(e.target.checked ? triage.apps.map((c) => c.app_id) : [])} /> Select all ({triage.apps.length})</label>
        <span className="muted" role="status">{plural(chosen.length, "application")} selected</span>
        <button type="button" disabled={!chosen.length || decide.isPending} onClick={() => run(chosen, "replicate")}>Replicate selected</button>
        <button type="button" className="secondary" disabled={!chosen.length || decide.isPending} onClick={() => run(chosen, "not_replicated")}>Not replicated (used only)</button>
      </div>
      <div className="triage">
        {triage.apps.map((card) => (
          <TriageCard key={card.app_id} card={card} sameAs={triage.same_as} chosen={chosen.includes(card.app_id)}
            onToggle={() => toggle(card.app_id)} onDecide={(d) => run([card.app_id], d)} busy={decide.isPending} />
        ))}
      </div>
    </>
  );
}

function TriageCard({ card, sameAs, chosen, onToggle, onDecide, busy }: {
  card: Schemas["TriageCard"]; sameAs: Schemas["AppRef"][]; chosen: boolean;
  onToggle: () => void; onDecide: (d: Decision) => void; busy: boolean;
}) {
  const navigate = useNavigate();
  const ranked = card.suggestions.filter((s) => s.can_merge);
  const others = sameAs.filter((a) => a.id !== card.app_id && !ranked.some((s) => s.app_id === a.id));
  const [into, setInto] = useState<string>(ranked[0] ? String(ranked[0].app_id) : "");
  const heading = `triage-${card.app_id}-h`;
  return (
    <article className="card triage-card" id={`app-${card.app_id}`} aria-labelledby={heading}>
      <h2 id={heading}>
        <input type="checkbox" aria-label={`Select ${card.name}`} checked={chosen} onChange={onToggle} />{" "}
        <Link to={`/apps/${card.app_id}`}>{card.name}</Link>{" "}
        <SurfaceBadge surface={card.surface} />{" "}
        <Badge kind={card.status}>{card.status_label}</Badge>
      </h2>
      <dl className="about">
        <dt>Identity</dt><dd>{card.identities.length ? card.identities.map((i) => (
          <span key={i.key}><code>{i.display}</code>{i.facts.company && <span className="muted"> · {i.facts.company}</span>}{i.facts.version && <span className="muted"> · v{i.facts.version}</span>}</span>
        )) : <span className="muted">none</span>}</dd>
        <dt>Steps</dt><dd>{plural(card.steps, "step")}{card.recordings.length > 0 && <> in {card.recordings.map((r, i) => (
          <span key={r.id}>{i > 0 && ", "}<Link to={`/recordings/${encodeURIComponent(r.id)}`}>{r.name}</Link></span>
        ))}</>}</dd>
        {card.screens.length > 0 && <><dt>Screens</dt><dd>{card.screens.join(" · ")}</dd></>}
        {card.controls.length > 0 && <><dt>Controls</dt><dd>{card.controls.join(" · ")}</dd></>}
      </dl>
      <div className="looks-like">
        <h3>Looks like</h3>
        {card.suggestions.length ? (
          <ol>{card.suggestions.map((s) => (
            <li key={s.app_id}><Link to={`/apps/${s.app_id}`}>{s.name}</Link> <span className="muted">({s.reasons.join(", ")})</span>{" "}
              {s.can_merge && <button type="button" className="secondary" aria-label={`${card.name} is the same as ${s.name}: preview the merge`}
                onClick={() => navigate(`/apps/${card.app_id}/merge?into=${s.app_id}`)}>Same as {s.name}</button>}</li>
          ))}</ol>
        ) : <p className="muted">Nothing you have looks like it: no shared host, similar name or vendor.</p>}
      </div>
      <div className="actions">
        <button type="button" disabled={busy} onClick={() => onDecide("replicate")} aria-label={`Replicate ${card.name}`}>Replicate</button>
        {(ranked.length > 0 || others.length > 0) && (
          <form className="inline" aria-label={`${card.name} is the same as`} onSubmit={(e) => { e.preventDefault(); if (into) navigate(`/apps/${card.app_id}/merge?into=${into}`); }}>
            <Combobox label="Same as" value={into} onChange={setInto} placeholder="Type an application's name"
              options={[...ranked.map((s) => ({ value: String(s.app_id), label: s.name, hint: "(suggested)" })),
                        ...others.map((a) => ({ value: String(a.id), label: a.name }))]} />{" "}
            <button className="secondary" disabled={!into}>Preview merge</button>
          </form>
        )}
        <button type="button" className="secondary" disabled={busy} onClick={() => onDecide("not_replicated")} aria-label={`${card.name}: not replicated (used only)`}>Not replicated (used only)</button>
      </div>
      <p className="muted">A merge is previewed before anything changes; replicate and not replicated can be reversed here.</p>
    </article>
  );
}
