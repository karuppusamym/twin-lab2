import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import { api, data, type Schemas } from "../api/client";
import { Badge } from "./ui";

type Verification = Schemas["Verification"];

const ACTIVE = new Set(["queued", "running"]);
const LABEL: Record<string, string> = {
  queued: "Queued", running: "Running", passed: "Passed: Tested", failed: "Not Tested", error: "Could not run",
};
const DIMENSIONS: [string, string][] = [
  ["behavioural", "Behavioural"], ["structural", "Structural"], ["visual", "Visual"], ["deployment", "Deployment"],
];

export function isActive(v: Verification | null | undefined) {
  return !!v && ACTIVE.has(v.state);
}

/** A verification (Verify as a job), polled while it is queued or running. When it ends the
 * release's page is refreshed, so its state, test results and reports show what it stored. */
export function VerificationProgress({ initial, showRelease = false }: { initial: Verification; showRelease?: boolean }) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["verification", initial.id],
    queryFn: async () => data(await api.GET("/api/v1/verifications/{verification_id}", { params: { path: { verification_id: initial.id } } })),
    initialData: initial,
    refetchInterval: (q) => (isActive(q.state.data) ? 2_000 : false),
  });
  const v = query.data;
  const wasActive = useRef(isActive(initial));
  useEffect(() => {
    if (wasActive.current && !isActive(v)) {
      wasActive.current = false;
      if (v.release_id) void client.invalidateQueries({ queryKey: ["release", v.release_id] });
    }
  }, [v, client]);
  const tone = v.state === "passed" ? "pass" : v.state === "failed" || v.state === "error" ? "fail" : v.state;
  return (
    <div className="verification" aria-live="polite" data-testid="verification">
      <p>
        <Badge kind={tone}>{LABEL[v.state] ?? v.state}</Badge>{" "}
        <span>Verification #{v.id}{isActive(v) && `: ${v.progress.stage}`}</span>
        {v.progress.total > 0 && <span className="muted"> · {v.progress.done} of {v.progress.total} scenario{v.progress.total === 1 ? "" : "s"} done</span>}
        {v.attempts > 1 && <span className="muted"> · started {v.attempts} times (a worker stopped while it ran)</span>}
      </p>
      {v.state === "queued" && <p className="muted">Waiting for the worker (<code>python -m twinlab2.worker</code>) to take it.</p>}
      {showRelease && (v.release_id
        ? <p><Link to={`/releases/${v.release_id}#verification`}>Open the release</Link></p>
        : isActive(v) && <p className="muted">The release is known once the worker has packaged the commit.</p>)}
      {v.progress.scenarios.length > 0 && (
        <table className="flow">
          <thead><tr><th scope="col">Scenario</th><th scope="col">State</th><th scope="col">Surface</th></tr></thead>
          <tbody>{v.progress.scenarios.map((s) => (
            <tr key={s.id}><td>{s.name}{s.why && <span className="muted"> — {s.why}</span>}</td>
              <td><Badge kind={s.result ?? s.state}>{s.result ?? s.state}</Badge></td><td>{s.surface ?? ""}</td></tr>
          ))}</tbody>
        </table>
      )}
      {!isActive(v) && v.reasons.length > 0 && (
        <><p><strong>{v.state === "error" ? "Why it could not run:" : "Why it is not Tested:"}</strong></p>
          <ul>{v.reasons.map((r) => <li key={r}>{r}</li>)}</ul></>
      )}
      {v.dimensions && (
        <table className="flow" aria-label="Verification by dimension">
          <thead><tr><th scope="col">Dimension</th><th scope="col">Result</th><th scope="col">What it covered</th></tr></thead>
          <tbody>{DIMENSIONS.map(([key, label]) => {
            const d = v.dimensions?.[key] as { status?: string; scope?: string } | undefined;
            return d ? <tr key={key}><td>{label}</td><td><Badge kind={d.status}>{d.status}</Badge></td><td>{d.scope}</td></tr> : null;
          })}</tbody>
        </table>
      )}
      {v.report?.url && <p><a href={v.report.url}>Report of this verification</a> <code className="muted">{v.report.sha256.slice(0, 12)}</code>{!showRelease && <span className="muted"> · every report is listed below</span>}</p>}
    </div>
  );
}
