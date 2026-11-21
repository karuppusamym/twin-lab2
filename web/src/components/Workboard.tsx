// A recording's workboard: one row per application the recording holds, with its identity,
// scope, steps, review, scenarios, package readiness, build and release, and one action; then
// the timeline as one swim lane per application over the recording's time axis.
import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";
import { Link } from "react-router-dom";
import { api, data, type Schemas } from "../api/client";
import { plural, short, when } from "../lib/format";
import { JourneyLink } from "./shared";
import { Badge, QueryView } from "./ui";

export function Workboard({ recordingId }: { recordingId: string }) {
  const board = useQuery({
    queryKey: ["recording", recordingId, "workboard"],
    queryFn: async () => data(await api.GET("/api/v1/recordings/{recording_id}/workboard", { params: { path: { recording_id: recordingId } } })),
  });
  return (
    <QueryView query={board} what="Loading the workboard">
      {(b) => (
        <>
          <WorkboardTable board={b} />
          <Timeline board={b} />
        </>
      )}
    </QueryView>
  );
}

function WorkboardTable({ board }: { board: Schemas["Workboard"] }) {
  const { counts } = board;
  return (
    <section id="workboard" aria-labelledby="workboard-h">
      <h2 id="workboard-h">Applications in this recording</h2>
      <p>
        {plural(counts.apps, "application")}: {counts.replicate} to replicate, {counts.not_replicated} not replicated
        {counts.needs_decision > 0 && <>, <b>{counts.needs_decision} waiting for a scope decision</b>{" "}
          <Link className="button" to={`/recordings/${encodeURIComponent(board.recording.id)}/triage`}>Decide scope</Link></>}.
      </p>
      <div className="scroll">
        <table className="flow workboard" aria-label="Workboard">
          <thead>
            <tr>
              <th scope="col">Application</th><th scope="col">Scope</th><th scope="col">Steps</th><th scope="col">Reviewed</th>
              <th scope="col">Scenarios</th><th scope="col">Package</th><th scope="col">Build</th><th scope="col">Release</th>
              <th scope="col">Next</th>
            </tr>
          </thead>
          <tbody>{board.apps.map((row) => <Row key={row.app_id} row={row} />)}</tbody>
        </table>
      </div>
      {board.download_href ? (
        <p className="package-line"><a className="button secondary" href={board.download_href}>Download all approved packages</a>{" "}
          <span className="muted">one ZIP: the latest approved package of each of the {plural(counts.approved_packages, "application")} here that you build, with a manifest</span></p>
      ) : <p className="muted">No application here has an approved package you can download yet.</p>}
    </section>
  );
}

function Row({ row }: { row: Schemas["WorkboardRow"] }) {
  const p = row.package;
  return (
    <tr data-app={row.app_id}>
      <th scope="row">
        <Link to={`/apps/${row.app_id}`}>{row.name}</Link>
        {row.identity && <div className="muted"><SurfaceBadge surface={row.identity.surface} /> <code>{row.identity.display}</code></div>}
      </th>
      <td><Badge kind={row.scope.status}>{row.scope.label}</Badge></td>
      <td>{row.steps}</td>
      <td>{row.reviewed === null ? <span className="muted">–</span> : row.reviewed
        ? <Badge kind="reviewed">{row.selected ?? 0} reviewed</Badge>
        : <Badge kind="suggested">{row.selected ?? 0} to review</Badge>}</td>
      <td>{row.scenarios ?? <span className="muted">–</span>}</td>
      <td>{p ? <PackageCell appId={row.app_id} name={row.name} p={p} /> : <span className="muted">–</span>}</td>
      <td>{row.build ? <Link to={`/builds/${row.build.id}`}><Badge kind={row.build.state}>{row.build.state_label}</Badge></Link> : <span className="muted">–</span>}
        {row.build && row.build.outstanding > 0 && <div><Badge kind="waiting_for_answer">{plural(row.build.outstanding, "question")}</Badge></div>}</td>
      <td>{row.release ? <Link to={`/releases/${row.release.id}`} aria-label={`Release ${row.release.version} of ${row.name}`}><Badge kind={row.release.state}>{row.release.state}</Badge> <code>{short(row.release.version)}</code></Link> : <span className="muted">–</span>}</td>
      <td>{row.action && (row.action.blocked
        ? <><JourneyLink href={row.action.href} className="button secondary">{row.action.label}</JourneyLink><div className="blocked">{row.action.blocked}</div></>
        : <JourneyLink href={row.action.href} className="button">{row.action.label}</JourneyLink>)}
        <div className="muted">{row.stage}</div></td>
    </tr>
  );
}

function PackageCell({ appId, name, p }: { appId: number; name: string; p: Schemas["PackageReadiness"] }) {
  const readiness = p.blockers === null ? null : p.blockers > 0
    ? <Badge kind="blocked">{plural(p.blockers, "blocker")}</Badge>
    : <Badge kind="passed">ready{p.warnings ? `, ${plural(p.warnings, "warning")}` : ""}</Badge>;
  return (
    <>
      {p.rev !== null ? <Link to={`/apps/${appId}/packages/${p.rev}`} aria-label={`Package revision ${p.rev} of ${name}`}>Revision {p.rev}</Link> : <span className="muted">none</span>}
      {p.state === "draft" && <> <Badge kind="draft">changes not frozen</Badge></>}
      {readiness && <div>{readiness}</div>}
      {p.reason && <div className="muted">{p.reason}</div>}
    </>
  );
}

export function SurfaceBadge({ surface }: { surface: string }) {
  return <span className={`surface surface-${surface || "unknown"}`}>{surface || "unknown"}</span>;
}

/** Where a run sits on the lane, in percent of the axis. */
function placement(run: Schemas["TimelineRun"], t: Schemas["Timeline"]) {
  if (t.axis === "time" && t.start && t.end && run.start && run.end) {
    const t0 = Date.parse(t.start), span = Math.max(Date.parse(t.end) - t0, 1);
    const left = ((Date.parse(run.start) - t0) / span) * 100;
    const width = ((Date.parse(run.end) - Date.parse(run.start)) / span) * 100;
    return { left, width };
  }
  const total = Math.max(t.total, 1);
  return { left: (run.from_index / total) * 100, width: ((run.to_index - run.from_index + 1) / total) * 100 };
}

function Timeline({ board }: { board: Schemas["Workboard"] }) {
  const t = board.timeline;
  const tableId = useId();
  const [shown, setShown] = useState<string>("");
  if (!t.lanes.length) return null;
  const surfaces = [...new Set(t.lanes.map((l) => l.surface || "unknown"))];
  return (
    <section id="timeline" aria-labelledby="timeline-h">
      <h2 id="timeline-h">Timeline</h2>
      <p className="muted">
        One lane per application{t.axis === "time" && t.start && t.end ? <>, from {when(t.start, 19)} to {t.end.slice(11, 19)}</> : ", in the order of the steps (some steps have no time)"}.
        Each bar is a run of consecutive steps in that application; hover or focus one to see its steps.
      </p>
      <p className="legend" aria-label="Surfaces">{surfaces.map((s) => <SurfaceBadge key={s} surface={s} />)}</p>
      <div className="swimlanes" role="list" aria-label="Applications over time">
        {t.lanes.map((lane) => (
          <div className="swimlane" role="listitem" key={lane.app_id} aria-label={`${lane.name}: ${plural(lane.steps, "step")} in ${plural(lane.runs.length, "run")}`}>
            <span className="swimlane-name"><Link to={`/apps/${lane.app_id}`}>{lane.name}</Link></span>
            <span className="swimlane-track">
              {lane.runs.map((run, i) => {
                const { left, width } = placement(run, t);
                const label = `${lane.name}: ${plural(run.steps, "step")}${run.start ? ` from ${run.start.slice(11, 19)}` : ""}${run.end && run.end !== run.start ? ` to ${run.end.slice(11, 19)}` : ""}. ${run.samples.join("; ")}`;
                const key = `${lane.app_id}-${i}`;
                return (
                  <button type="button" key={key} className={`run surface-${lane.surface || "unknown"}`} title={label} aria-label={label}
                    style={{ left: `${Math.min(left, 99.4)}%`, width: `max(${width}%, 0.6%)` }}
                    onMouseEnter={() => setShown(label)} onFocus={() => setShown(label)} onClick={() => setShown(label)} />
                );
              })}
            </span>
            <span className="muted swimlane-count">{lane.steps}</span>
          </div>
        ))}
      </div>
      <p className="muted" aria-live="polite" id="timeline-hover">{shown || "Hover or focus a bar to see its steps."}</p>
      {board.shared_hosts.length > 0 && (
        <div className="shared-hosts" id="shared-hosts">
          <h3>Shared hosts</h3>
          <ul>{board.shared_hosts.map((h) => <li key={h.host}>{h.text}</li>)}</ul>
        </div>
      )}
      <details>
        <summary>The timeline as a table</summary>
        <table className="flow" id={tableId} aria-label="Timeline runs">
          <thead><tr><th scope="col">Application</th><th scope="col">From</th><th scope="col">To</th><th scope="col">Steps</th><th scope="col">Some of its steps</th></tr></thead>
          <tbody>{t.lanes.flatMap((lane) => lane.runs.map((run, i) => (
            <tr key={`${lane.app_id}-${i}`}>
              <td>{lane.name}</td><td>{run.start?.slice(11, 19) ?? `#${run.from_index + 1}`}</td><td>{run.end?.slice(11, 19) ?? `#${run.to_index + 1}`}</td>
              <td>{run.steps}</td><td>{run.samples.join("; ")}</td>
            </tr>
          )))}</tbody>
        </table>
      </details>
    </section>
  );
}
