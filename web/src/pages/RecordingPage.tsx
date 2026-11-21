import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, data, type Schemas } from "../api/client";
import { FlowTable } from "../components/shared";
import { ActionButton, Badge, ErrorText, QueryView, useAction, useTitle } from "../components/ui";
import { capitalize, plural, short, when, words } from "../lib/format";
import { useCan } from "../session";

export function RecordingPage() {
  const recordingId = useParams().recordingId ?? "";
  const rec = useQuery({
    queryKey: ["recording", recordingId],
    queryFn: async () => data(await api.GET("/api/v1/recordings/{recording_id}", { params: { path: { recording_id: recordingId } } })),
    // upload progress: refreshed while the upload is not complete
    refetchInterval: (q) => (q.state.data?.upload && !q.state.data.upload.complete ? 5_000 : false),
  });
  useTitle(rec.data?.name);
  return <QueryView query={rec}>{(r) => <RecordingView rec={r} />}</QueryView>;
}

function RecordingView({ rec }: { rec: Schemas["RecordingDetail"] }) {
  const { status, refs } = rec;
  const c = status.completeness;
  return (
    <>
      <h1>{rec.name}</h1>
      <section id="details" aria-labelledby="details-h">
        <h2 id="details-h">Recording details</h2>
        <p id="completeness"><Badge kind={c && c.overall === "complete" ? "recorded" : c ? "discovered" : "packaged"}>{status.label}</Badge> {status.summary}.</p>
        {c?.recovered && (
          <p className="warning" id="recovered"><b>Recovered after the recorder stopped unexpectedly.</b> The recording was rebuilt from its journal
            {c.loss_window_seconds != null && `: up to ${plural(c.loss_window_seconds, "second")} before the interruption may be lost`}. Check the events around the interruption before relying on them.</p>
        )}
        {status.attention ? (
          <div className="warning" id="attention">
            <p><b>Needs attention: this recording stopped, but it is {c?.overall}.</b> A stopped recording is not complete by itself. Re-record the missing part, or accept the recording as it is if what it has is enough.</p>
            <ActionButton label="Accept it as it is" run={async () => data(await api.POST("/api/v1/recordings/{recording_id}/completeness/accept", { params: { path: { recording_id: rec.id } } }))} />
          </div>
        ) : status.accepted && <p className="muted">Accepted as it is by {status.accepted_by} on {when(status.accepted_at)}.</p>}
        {c && (
          <table className="flow" id="channels">
            <thead><tr><th scope="col">Channel</th><th scope="col">Status</th><th scope="col">Affected time</th><th scope="col">Notes</th></tr></thead>
            <tbody>
              {Object.entries(c.channels).map(([name, ch]) => (
                <tr key={name}>
                  <td>{capitalize(name)}</td><td>{words(ch.status)}</td>
                  <td>{ch.gaps.map((g, i) => <span key={i}>{g.from.slice(11, 16)}–{g.to.slice(11, 16)}{g.reason && <span className="muted"> ({g.reason})</span>}{i < ch.gaps.length - 1 && <br />}</span>)}</td>
                  <td>{ch.notes.join("; ")}</td>
                </tr>
              ))}
              <tr><td>Network evidence</td><td>{c.network_evidence}</td><td /><td /></tr>
            </tbody>
          </table>
        )}
        {refs && refs.ranges.length > 0 && (
          <div className="lanes" aria-label="Affected time ranges">
            <p className="muted">{refs.session.started?.slice(11, 16)} – {refs.session.ended?.slice(11, 16)}</p>
            {refs.ranges.map((lane) => (
              <div className="lane" key={lane.label}>
                <span className="lane-name">{lane.label}</span>
                <span className={`lane-bar ${lane.status}`}>{lane.gaps.map((g, i) => g.left != null && (
                  <span key={i} className="gap" style={{ left: `${g.left}%`, width: `${g.width ?? 0}%` }} title={`${g.text} ${g.reason ?? ""}`} />
                ))}</span>
                <span className="muted">{words(lane.status)}{lane.gaps.map((g) => ` · ${g.text}`).join("")}</span>
              </div>
            ))}
          </div>
        )}
        <h3>Preflight</h3>
        {status.preflight ? (
          <>
            <p>{status.preflight.ready ? "Ready to record" : "Not ready: recording started with failed checks"}.</p>
            <ul className="checks" id="preflight">{status.preflight.checks.map((ck) => (
              <li key={ck.name} className={ck.ok ? "ok" : "failed"}>{ck.ok ? "✓" : "✗"} {ck.name}{ck.detail && <span className="muted"> {ck.detail}</span>}</li>
            ))}</ul>
          </>
        ) : <p className="muted">No preflight results{status.workstation ? " (older recorder)" : " for an imported event log"}.</p>}
        <h3>Upload</h3>
        {rec.upload ? (
          <p id="upload">{rec.upload.text}{rec.upload.percent != null && <> <progress max={100} value={rec.upload.percent}>{rec.upload.percent}%</progress></>}</p>
        ) : <p className="muted" id="upload">Upload progress: not available.</p>}
        <h3>Evidence preview</h3>
        {rec.preview.length ? rec.preview.map((p) => (
          <div key={p.app_id}>
            <p><Link to={`/apps/${p.app_id}`}>{p.name}</Link>: {plural(p.count, "event")} selected{!p.reviewed && ", not yet reviewed"}{p.suggested > 0 && `, ${p.suggested} new`}.</p>
            {p.events.length > 0 && <EventTable events={p.events} />}
            {p.count > p.events.length && <p className="muted">and {p.count - p.events.length} more.</p>}
          </div>
        )) : <p className="muted">No application has evidence from this recording yet.</p>}
      </section>
      {refs && <RecordedApps refs={refs} recordingId={rec.id} />}
      <Assignment rec={rec} />
    </>
  );
}

function EventTable({ events }: { events: Schemas["Event"][] }) {
  return (
    <div className="scroll">
      <table className="flow">
        <thead><tr><th scope="col">#</th><th scope="col">Action</th><th scope="col">Control</th><th scope="col">Label</th><th scope="col">Screen</th><th scope="col"><span className="visually-hidden">Notes</span></th></tr></thead>
        <tbody>{events.map((e) => (
          <tr key={e.event_id}>
            <td>{e.seq}</td><td>{e.action}{e.key && ` ${e.key}`}</td><td>{e.control ?? ""}</td><td>{e.label ?? ""}</td><td>{e.screen ?? ""}</td>
            <td>{e.in_gap && <Badge kind="discovered">in an interrupted period</Badge>}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

/** The applications this recording holds, each with its own steps and its own package. */
function RecordedApps({ refs, recordingId }: { refs: Schemas["RecordingRefs"]; recordingId: string }) {
  const can = useCan();
  return (
    <section aria-labelledby="recorded-h" id="recorded-apps">
      <h2 id="recorded-h">Recorded applications</h2>
      <p className="muted">Recorded on {refs.machine ?? "a machine since revoked"}{refs.session.started && `, ${when(refs.session.started)}`}
        {refs.session.ended && ` to ${refs.session.ended.slice(11, 16)}`}{refs.automation && ` · for ${refs.automation}`}
        {refs.session.state === "recovered" && " · recovered after the recorder stopped"}. The recording stays on that machine at <code>recordings/{refs.session.id}</code>.</p>
      {refs.contract && (
        <p className="muted">Sent by {refs.contract.recorder} {refs.contract.recorder_version} · recorder contract {refs.contract.contract_version}
          {refs.contract.twinlab_commit && <> · recorder source <code>{short(refs.contract.twinlab_commit)}</code></>}.</p>
      )}
      <ol className="timeline">{refs.timeline.map((run, i) => (
        <li key={i} className={run.app === "presence" ? "presence" : ""}>{run.name} <span className="muted">{plural(run.steps, "step")}</span></li>
      ))}</ol>
      {refs.apps.map((a, i) => (
        <details key={`${a.app_id}/${a.key}`} open={i === 0} aria-label={a.name} data-app={a.app_id}>
          <summary>
            <Link to={`/apps/${a.app_id}`}>{a.name}</Link> <Badge kind={a.status}>{a.status_label}</Badge>{" "}
            <span className="muted">{a.level} · <code>{a.key}</code> · {plural(a.steps.length, "step")}</span>
          </summary>
          <FlowTable steps={a.steps} caption={`Steps of ${a.name}`} />
          <p className="package-line">
            {a.latest_package ? (
              <>Package: <Link to={`/apps/${a.app_id}/packages/${a.latest_package.rev}`}>revision {a.latest_package.rev}</Link>{" "}
                <span className="muted">approved by {a.latest_package.approved_by} · {when(a.latest_package.approved_at)} · <code>{short(a.latest_package.sha256)}</code></span>
                {can("builder", a.app_id) && <> · <a href={`/api/v1/apps/${a.app_id}/packages/${a.latest_package.rev}/download`}>Download</a></>}</>
            ) : <span className="muted">No builder-approved package of {a.name} yet.</span>}
          </p>
        </details>
      ))}
      {refs.apps.some((a) => a.latest_package && can("builder", a.app_id)) && (
        <p className="package-line">
          <a href={`/api/v1/recordings/${encodeURIComponent(recordingId)}/packages/download`}>Download the packages of this recording</a>{" "}
          <span className="muted">one ZIP: the latest approved package of each application here that you build, and a manifest of all of them</span>
        </p>
      )}
    </section>
  );
}

/** Assigning this recording's events to one application the user owns (the owner's review). */
function Assignment({ rec }: { rec: Schemas["RecordingDetail"] }) {
  const [params, setParams] = useSearchParams();
  const appId = Number(params.get("app_id")) || null;
  const [choice, setChoice] = useState(appId ? String(appId) : "");
  const allowed = rec.assignable_apps.some((a) => a.id === appId);
  return (
    <section aria-labelledby="assign-h" id="assign">
      <h2 id="assign-h">Assign events</h2>
      <p>{plural(rec.events.length, "event")}. Select the events and describe the outcome for the chosen demo app.</p>
      {rec.assignable_apps.length ? (
        <>
          <form id="app-picker" aria-label="Choose a demo app" onSubmit={(e) => { e.preventDefault(); if (choice) setParams({ app_id: choice }); }}>
            <label>Demo app
              <select required value={choice} onChange={(e) => setChoice(e.target.value)}>
                <option value="">Choose an app</option>
                {rec.assignable_apps.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            </label>
            <button>Open assignment</button>
          </form>
          {appId && allowed && <AssignmentForm rec={rec} appId={appId} />}
        </>
      ) : <p>Create a demo app in the catalog to assign events.</p>}
    </section>
  );
}

function AssignmentForm({ rec, appId }: { rec: Schemas["RecordingDetail"]; appId: number }) {
  const navigate = useNavigate();
  const saved = useQuery({
    queryKey: ["recording", rec.id, "assignment", appId],
    queryFn: async () => data(await api.GET("/api/v1/recordings/{recording_id}/assignments/{app_id}", { params: { path: { recording_id: rec.id, app_id: appId } } })),
  });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [notes, setNotes] = useState("");
  useEffect(() => {
    if (saved.data) {
      setSelected(new Set(saved.data.selected));
      setNotes(saved.data.notes);
    }
  }, [saved.data]);
  const groups = useMemo(() => {
    const out = new Map<string, Schemas["Event"][]>();
    for (const e of rec.events) out.set(e.source, [...(out.get(e.source) ?? []), e]);
    return [...out.entries()];
  }, [rec.events]);
  const save = useAction(async () => data(await api.PUT("/api/v1/recordings/{recording_id}/assignments/{app_id}", {
    params: { path: { recording_id: rec.id, app_id: appId } }, body: { events: [...selected], notes: notes.trim() },
  })), () => navigate(`/apps/${appId}`));
  const toggle = (ids: string[], on: boolean) => setSelected((s) => {
    const next = new Set(s);
    for (const id of ids) if (on) next.add(id); else next.delete(id);
    return next;
  });
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate(undefined); };
  return (
    <QueryView query={saved}>
      {(a) => {
        const suggested = new Set(a.suggested);
        return (
          <form aria-label="Assignment" onSubmit={submit}>
            {a.saved && !a.reviewed && <p className="muted">Selected automatically from the recording: not yet reviewed. Save the assignment to confirm it.</p>}
            {suggested.size > 0 && <p className="muted">{plural(suggested.size, "event")} new in a later send of this recording, not yet reviewed: they are not in the build pack until you select them and save.</p>}
            <label>Outcome and build notes <textarea rows={4} value={notes} onChange={(e) => setNotes(e.target.value)} /></label>
            {groups.map(([source, events]) => {
              const ids = events.map((e) => e.event_id);
              const all = ids.every((id) => selected.has(id));
              return (
                <fieldset key={source}>
                  <legend>{source} ({events.length})</legend>
                  <button type="button" className="secondary" onClick={() => toggle(ids, !all)}>{all ? "Clear all" : "Select all"}</button>
                  <div className="events">
                    {events.map((ev) => (
                      <label key={ev.event_id}>
                        <input type="checkbox" checked={selected.has(ev.event_id)} onChange={(e) => toggle([ev.event_id], e.target.checked)} />
                        {ev.seq} · {ev.action}
                        {suggested.has(ev.event_id) && <> <Badge kind="discovered">new, not yet reviewed</Badge></>}
                        {ev.in_gap && <> <Badge kind="discovered">in an interrupted period</Badge></>}
                      </label>
                    ))}
                  </div>
                </fieldset>
              );
            })}
            <ErrorText error={save.error} />
            <button disabled={save.isPending || selected.size === 0}>Save assignment</button>
          </form>
        );
      }}
    </QueryView>
  );
}
