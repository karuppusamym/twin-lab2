import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useDeferredValue, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, data, type Schemas } from "../api/client";
import { Combobox } from "../components/Combobox";
import { EventChooser, NO_SCREEN, type ChooserEvent } from "../components/EventChooser";
import { FlowTable } from "../components/shared";
import { ActionButton, Badge, ConflictView, Crumbs, DirtyNote, ErrorText, QueryView, conflictOf, useAction, useDraft, useLeaveWarning, useTitle } from "../components/ui";
import { Workboard } from "../components/Workboard";
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
  const [params] = useSearchParams();
  const assigning = params.has("app_id");
  return (
    <>
      <Crumbs><Link to="/recordings">Recordings</Link></Crumbs>
      <h1>{rec.name}</h1>
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
      <Workboard recordingId={rec.id} />
      <details id="details" className="details-section">
        <summary><span className="summary-title">Details</span> <span className="muted">channels, preflight, upload, every application&apos;s recorded steps</span></summary>
        <section aria-labelledby="channels-h">
          <h3 id="channels-h">Channels</h3>
          {c ? (
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
          ) : <p className="muted">No channel report for this recording.</p>}
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
        </section>
        <section aria-labelledby="preflight-h">
          <h3 id="preflight-h">Preflight</h3>
          {status.preflight ? (
            <>
              <p>{status.preflight.ready ? "Ready to record" : "Not ready: recording started with failed checks"}.</p>
              <ul className="checks" id="preflight">{status.preflight.checks.map((ck) => (
                <li key={ck.name} className={ck.ok ? "ok" : "failed"}>{ck.ok ? "✓" : "✗"} {ck.name}{ck.detail && <span className="muted"> {ck.detail}</span>}</li>
              ))}</ul>
            </>
          ) : <p className="muted">No preflight results{status.workstation ? " (older recorder)" : " for an imported event log"}.</p>}
        </section>
        <section aria-labelledby="upload-h">
          <h3 id="upload-h">Upload</h3>
          {rec.upload ? (
            <p id="upload">{rec.upload.text}{rec.upload.percent != null && <> <progress max={100} value={rec.upload.percent}>{rec.upload.percent}%</progress></>}</p>
          ) : <p className="muted" id="upload">Upload progress: not available.</p>}
        </section>
        <section aria-labelledby="preview-h">
          <h3 id="preview-h">Evidence preview</h3>
          {rec.preview.length ? rec.preview.map((p) => (
            <div key={p.app_id}>
              <p><Link to={`/apps/${p.app_id}`}>{p.name}</Link>: {plural(p.count, "event")} selected{!p.reviewed && ", not yet reviewed"}{p.suggested > 0 && `, ${p.suggested} new`}.</p>
              {p.events.length > 0 && <EventTable events={p.events} />}
              {p.count > p.events.length && <p className="muted">and {p.count - p.events.length} more.</p>}
            </div>
          )) : <p className="muted">No application has evidence from this recording yet.</p>}
        </section>
        {refs && <RecordedApps refs={refs} recordingId={rec.id} />}
      </details>
      <details id="assign" className="details-section" open={assigning}>
        <summary><span className="summary-title">Assign events</span> <span className="muted">select this recording&apos;s events for one application and describe its outcome</span></summary>
        <Assignment rec={rec} />
      </details>
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
      <h3 id="recorded-h">Recorded applications</h3>
      <p className="muted">Recorded on {refs.machine ?? "a machine since revoked"}{refs.session.started && `, ${when(refs.session.started)}`}
        {refs.session.ended && ` to ${refs.session.ended.slice(11, 16)}`}{refs.automation && ` · for ${refs.automation}`}
        {refs.session.state === "recovered" && " · recovered after the recorder stopped"}. The recording stays on that machine at <code>recordings/{refs.session.id}</code>.</p>
      {refs.contract && (
        <p className="muted">Sent by {refs.contract.recorder} {refs.contract.recorder_version} · recorder contract {refs.contract.contract_version}
          {refs.contract.twinlab_commit && <> · recorder source <code>{short(refs.contract.twinlab_commit)}</code></>}.</p>
      )}
      <p className="muted">The order the recorder saw the applications in, as runs of steps. A run named after several applications is a host they share whose steps are told apart by path (the longest path prefix wins).</p>
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
                {can("builder", a.app_id) && <> · <a href={`/api/v1/apps/${a.app_id}/packages/${a.latest_package.rev}/download`} aria-label={`Download revision ${a.latest_package.rev} of ${a.name}`}>Download</a></>}</>
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
    <section aria-label="Assign events" id="assign-events">
      <p>{plural(rec.events_total, "event")}. Select the events and describe the outcome for the chosen demo app.</p>
      {rec.assignable_apps.length ? (
        <>
          <form id="app-picker" aria-label="Choose a demo app" onSubmit={(e) => { e.preventDefault(); if (choice) setParams({ app_id: choice }); }}>
            <Combobox label="Demo app" required value={choice} onChange={setChoice} placeholder="Type an application's name"
              options={rec.assignable_apps.map((a) => ({ value: String(a.id), label: a.name }))} />
            <button>Open assignment</button>
          </form>
          {appId && allowed && <AssignmentForm key={appId} rec={rec} appId={appId} appName={rec.assignable_apps.find((a) => a.id === appId)?.name ?? ""} />}
        </>
      ) : <p>Create a demo app in the catalog to assign events.</p>}
    </section>
  );
}

type AssignmentDraft = { selected: string[]; notes: string };
const draftOf = (a: Schemas["Assignment"]): AssignmentDraft => ({ selected: [...a.selected].sort(), notes: a.notes });
const PAGE = 100;

function AssignmentForm({ rec, appId, appName }: { rec: Schemas["RecordingDetail"]; appId: number; appName: string }) {
  const saved = useQuery({
    queryKey: ["recording", rec.id, "assignment", appId],
    queryFn: async () => data(await api.GET("/api/v1/recordings/{recording_id}/assignments/{app_id}", { params: { path: { recording_id: rec.id, app_id: appId } } })),
  });
  return <QueryView query={saved} what="Loading the assignment">{(a) => <AssignmentEditor rec={rec} appId={appId} appName={appName} server={a} />}</QueryView>;
}

function AssignmentEditor({ rec, appId, appName, server }: { rec: Schemas["RecordingDetail"]; appId: number; appName: string; server: Schemas["Assignment"] }) {
  const navigate = useNavigate();
  const draft = useDraft<AssignmentDraft>(draftOf(server), server.revision);
  useLeaveWarning(draft.dirty);
  const selected = new Set(draft.value.selected);
  const families = [["recording", rec.id], ["recordings"], ["apps"], ["app", appId]];
  const path = { recording_id: rec.id, app_id: appId };
  const save = useAction(async (revision: number) => data(await api.PUT("/api/v1/recordings/{recording_id}/assignments/{app_id}", {
    params: { path }, body: { events: draft.value.selected, notes: draft.value.notes.trim(), expected_revision: revision },
  })), (a) => { draft.reset(draftOf(a), a.revision); navigate(`/apps/${appId}`); }, families);
  const remove = useAction(async () => data(await api.DELETE("/api/v1/recordings/{recording_id}/assignments/{app_id}", {
    params: { path, query: { expected_revision: draft.revision } },
  })), (a) => draft.reset(draftOf(a), a.revision), families);
  const theirs = conflictOf<Schemas["Assignment"]>(save.error) ?? conflictOf<Schemas["Assignment"]>(remove.error);
  const setSelected = (ids: string[], on: boolean) => draft.set((d) => {
    const next = new Set(d.selected);
    for (const id of ids) if (on) next.add(id); else next.delete(id);
    return { ...d, selected: [...next].sort() };
  });
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate(draft.revision); };
  const suggested = new Set(server.suggested);
  return (
    <form aria-label="Assignment" onSubmit={submit}>
      <h3>Events of {appName} <DirtyNote dirty={draft.dirty} /></h3>
      {server.removed && <p className="muted" id="removed-note">Removed from this recording: {appName} uses none of its events or notes. Select events and save to assign it again.</p>}
      {server.saved && !server.reviewed && <p className="muted">Selected automatically from the recording: not yet reviewed. Save the assignment to confirm it.</p>}
      {suggested.size > 0 && <p className="muted">{plural(suggested.size, "event")} new in a later send of this recording, not yet reviewed: they are not in the build pack until you select them and save.</p>}
      <label>Outcome and build notes <textarea rows={4} value={draft.value.notes} onChange={(e) => { const notes = e.target.value; draft.set((d) => ({ ...d, notes })); }} /></label>
      <EventPicker rec={rec} selected={selected} suggested={suggested} onSelect={setSelected} />
      {theirs ? (
        <ConflictView
          busy={save.isPending}
          rows={[
            { label: "Selected events", mine: plural(draft.value.selected.length, "event"), theirs: `${plural(theirs.selected.length, "event")}${theirs.removed ? " (assignment removed)" : ""}` },
            { label: "Events only in yours", mine: String(draft.value.selected.filter((id) => !theirs.selected.includes(id)).length), theirs: "0" },
            { label: "Events only in theirs", mine: "0", theirs: String(theirs.selected.filter((id) => !selected.has(id)).length) },
            { label: "Notes", mine: draft.value.notes.trim(), theirs: theirs.notes },
          ]}
          onKeepMine={() => { remove.reset(); save.mutate(theirs.revision); }}
          onTakeTheirs={() => { draft.reset(draftOf(theirs), theirs.revision); save.reset(); remove.reset(); }}
        />
      ) : <><ErrorText error={save.error} /><ErrorText error={remove.error} /></>}
      <div className="actions">
        <button disabled={save.isPending || selected.size === 0}>Save assignment</button>
        <span className="muted">{plural(selected.size, "event")} selected{selected.size === 0 && server.saved ? ": to assign none, remove the assignment below" : ""}.</span>
      </div>
      {server.saved && (
        <div className="remove-assignment">
          <p className="muted">Removing takes {appName} off this recording: the next draft package of {appName} leaves out all of this recording&apos;s events and notes, and a later send of the same recording does not select them again. Packages already frozen keep them. The removal is recorded in the application&apos;s history.</p>
          <button type="button" className="destructive" disabled={remove.isPending}
            onClick={() => { if (window.confirm(`Remove the assignment of ${appName} from this recording? Its next package will not use this recording.`)) remove.mutate(undefined); }}>
            Remove this app&apos;s assignment from the recording
          </button>
        </div>
      )}
    </form>
  );
}

/** The recording's events in the shared chooser: the server filters and pages them. */
function EventPicker({ rec, selected, suggested, onSelect }: {
  rec: Schemas["RecordingDetail"]; selected: Set<string>; suggested: Set<string>; onSelect: (ids: string[], on: boolean) => void;
}) {
  const [q, setQ] = useState("");
  const [source, setSource] = useState("");
  const [screen, setScreen] = useState("");
  const [offset, setOffset] = useState(0);
  const query = useDeferredValue(q.trim());
  const filters = { q: query || undefined, source: source || undefined, screen: screen ? (screen === NO_SCREEN ? "" : screen) : undefined };
  const events = useQuery({
    queryKey: ["recording", rec.id, "events", filters, offset],
    queryFn: async () => data(await api.GET("/api/v1/recordings/{recording_id}/events", {
      params: { path: { recording_id: rec.id }, query: { ...filters, offset, limit: PAGE } },
    })),
    placeholderData: keepPreviousData,
  });
  const matching = useAction(async (on: boolean) => {
    const all = data(await api.GET("/api/v1/recordings/{recording_id}/events", {
      params: { path: { recording_id: rec.id }, query: { ...filters, ids: true, limit: 1 } },
    }));
    onSelect(all.ids ?? [], on);
  }, undefined, []);
  const page = events.data;
  if (!page) return <QueryView query={events} what="Loading events">{() => null}</QueryView>;
  const rows: ChooserEvent[] = page.events.map((ev) => ({
    id: ev.event_id, seq: ev.seq, action: `${ev.action}${ev.key ? ` ${ev.key}` : ""}`, label: ev.label, screen: ev.screen,
    flow: ev.app ?? ev.source, time: ev.time,
    note: <>{suggested.has(ev.event_id) && <Badge kind="suggested">new, not yet reviewed</Badge>}{ev.in_gap && <> <Badge kind="interrupted">in an interrupted period</Badge></>}</>,
  }));
  return (
    <>
      <ErrorText error={events.error} />
      <EventChooser
        legend="Recording events" events={rows} page={page.page} selected={selected} onSelect={onSelect}
        q={q} onQ={(v) => { setQ(v); setOffset(0); }} loading={events.isFetching}
        flow={{ label: "Application", value: source, onChange: (v) => { setSource(v); setScreen(""); setOffset(0); },
                options: page.sources.map((src) => ({ value: src.source, label: src.source, count: src.count })) }}
        screen={{ label: "Screen", value: screen, onChange: (v) => { setScreen(v); setOffset(0); },
                  options: page.screens.map((sc) => ({ value: sc.screen ?? NO_SCREEN, label: sc.screen ?? "No screen", count: sc.count })) }}
        onSelectMatching={(on) => matching.mutate(on)} matchingBusy={matching.isPending} matchingError={matching.error}
        onOffset={setOffset}
      />
    </>
  );
}
