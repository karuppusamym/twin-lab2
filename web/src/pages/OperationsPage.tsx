import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api, data, type Schemas } from "../api/client";
import { ErrorText, QueryView, useAction, useTitle } from "../components/ui";

type Policy = Schemas["RetentionPolicy"];
const KINDS: [keyof Policy & string, string][] = [
  ["raw_recordings", "Raw recordings (uploaded bundles)"],
  ["evidence", "Uploaded evidence files"],
  ["frozen_packages", "Frozen builder packages (not the latest, not released)"],
  ["build_logs", "Build conversation messages"],
  ["audit", "Audit history (at least 30 days)"],
];
const operationsKey = ["admin-operations"];

function bytes(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KiB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MiB`;
  return `${(n / 1024 ** 3).toFixed(2)} GiB`;
}

export function OperationsPage() {
  useTitle("Operations");
  const ops = useQuery({ queryKey: operationsKey, queryFn: async () => data(await api.GET("/api/v1/admin/operations")) });
  return (
    <>
      <h1>Operations</h1>
      <p className="muted">Storage and encryption, retention of stored data, deletions and purges, and the audit export. Runbooks: <code>docs/operations/</code>.</p>
      <QueryView query={ops}>{(o) => <OperationsView ops={o} />}</QueryView>
      <AuditExport />
      <Tombstones />
      <Purge />
      <DeleteRecording />
    </>
  );
}

function OperationsView({ ops }: { ops: Schemas["Operations"] }) {
  return (
    <>
      <section aria-labelledby="storage-h">
        <h2 id="storage-h">Storage and monitoring</h2>
        <dl className="release-details">
          {Object.entries(ops.storage).map(([k, v]) => <div key={k} style={{ display: "contents" }}><dt>{k}</dt><dd>{String(v)}</dd></div>)}
          <dt>Metrics</dt><dd>{ops.metrics.port ? `GET /metrics on internal port ${ops.metrics.port}` : "no internal port"}{ops.metrics.token ? ", or with the bearer token" : ""}</dd>
          <dt>Logs</dt><dd>{ops.log_format === "json" ? "JSON lines" : "text"}</dd>
          <dt>Tracing</dt><dd>{ops.tracing ? "on (OpenTelemetry)" : "off"}</dd>
          <dt>Job queue</dt><dd>{Object.entries(ops.queue).map(([s, n]) => `${s}: ${n}`).join(", ") || "empty"}</dd>
        </dl>
      </section>
      <RetentionForm policy={ops.policy} plan={ops.plan} />
      <Runs runs={ops.runs} />
    </>
  );
}

function RetentionForm({ policy, plan }: { policy: Policy; plan: Schemas["RetentionPlan"] }) {
  const [days, setDays] = useState<Record<string, string>>(() =>
    Object.fromEntries(KINDS.map(([k]) => [k, policy[k] == null ? "" : String(policy[k])])));
  const [daily, setDaily] = useState(policy.schedule === "daily");
  const save = useAction(async () => data(await api.PUT("/api/v1/admin/retention", {
    body: { ...Object.fromEntries(KINDS.map(([k]) => [k, days[k] ? Number(days[k]) : null])), schedule: daily ? "daily" : "off" },
  })), undefined, [operationsKey]);
  const run = useAction(async (dry: boolean) => data(await api.POST("/api/v1/admin/retention/runs", { body: { dry_run: dry } })),
    undefined, [operationsKey]);
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate(undefined); };
  return (
    <section aria-labelledby="retention-h">
      <h2 id="retention-h">Retention</h2>
      <p className="muted">Days to keep each kind; empty keeps it forever. A run deletes what is older, keeps a tombstone of each deleted object and writes the audit history.{policy.updated_by ? ` Last changed by ${policy.updated_by}.` : ""}</p>
      <form aria-label="Retention policy" onSubmit={submit}>
        <table className="flow">
          <thead><tr><th scope="col">Kind</th><th scope="col">Keep (days)</th><th scope="col">Would go now</th></tr></thead>
          <tbody>{KINDS.map(([k, label]) => (
            <tr key={k}>
              <td>{label}</td>
              <td><input type="number" min={k === "audit" ? 30 : 1} max={36500} aria-label={`Days to keep: ${label}`} value={days[k]}
                onChange={(e) => setDays({ ...days, [k]: e.target.value })} /></td>
              <td>{plan[k as keyof typeof plan].count} ({bytes(plan[k as keyof typeof plan].bytes)})</td>
            </tr>
          ))}</tbody>
        </table>
        <label className="check"><input type="checkbox" checked={daily} onChange={(e) => setDaily(e.target.checked)} /> Run every day</label>
        <ErrorText error={save.error} />
        <button disabled={save.isPending}>Save policy</button>
      </form>
      <p>
        <button type="button" className="secondary" disabled={run.isPending} onClick={() => run.mutate(true)}>Dry run</button>{" "}
        <button type="button" className="destructive" disabled={run.isPending}
          onClick={() => { if (window.confirm("Delete everything older than the policy now? Tombstones stay; the bytes cannot be brought back.")) run.mutate(false); }}>Apply now</button>
      </p>
      <ErrorText error={run.error} />
    </section>
  );
}

function Runs({ runs }: { runs: Schemas["RetentionRun"][] }) {
  return (
    <section aria-labelledby="runs-h">
      <h2 id="runs-h">Retention runs</h2>
      <table className="flow">
        <thead><tr><th scope="col">Run</th><th scope="col">By</th><th scope="col">Kind of run</th><th scope="col">State</th><th scope="col">Result</th></tr></thead>
        <tbody>{runs.length ? runs.map((r) => (
          <tr key={r.id}>
            <td>#{r.id} {r.created_at.replace("T", " ").slice(0, 19)}</td><td>{r.requested_by}</td><td>{r.dry_run ? "dry run" : "applied"}</td><td>{r.state}</td>
            <td>{r.error ?? (r.result ? Object.entries(r.result).filter(([, v]) => v.count).map(([k, v]) => `${k}: ${v.count}`).join(", ") || "nothing" : "…")}</td>
          </tr>
        )) : <tr><td colSpan={5}>No runs yet.</td></tr>}</tbody>
      </table>
    </section>
  );
}

function AuditExport() {
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [format, setFormat] = useState("csv");
  const query = new URLSearchParams({ format, ...(since ? { since } : {}), ...(until ? { until } : {}) });
  return (
    <section aria-labelledby="audit-h">
      <h2 id="audit-h">Audit export</h2>
      <form aria-label="Audit export" onSubmit={(e) => e.preventDefault()}>
        <label>From (inclusive) <input type="date" value={since} onChange={(e) => setSince(e.target.value)} /></label>
        <label>Until (exclusive) <input type="date" value={until} onChange={(e) => setUntil(e.target.value)} /></label>
        <label>Format <select value={format} onChange={(e) => setFormat(e.target.value)}><option value="csv">CSV</option><option value="json">JSON lines</option></select></label>
      </form>
      <p><a href={`/api/v1/admin/audit-export?${query}`}>Download the audit history</a></p>
    </section>
  );
}

function Tombstones() {
  const stones = useQuery({ queryKey: ["admin-tombstones"], queryFn: async () => data(await api.GET("/api/v1/admin/tombstones")) });
  return (
    <section aria-labelledby="tombstones-h">
      <h2 id="tombstones-h">Deleted</h2>
      <QueryView query={stones}>{(t) => <TombstoneTable tombstones={t.tombstones} />}</QueryView>
    </section>
  );
}

export function TombstoneTable({ tombstones }: { tombstones: Schemas["Tombstone"][] }) {
  return (
    <table className="flow">
      <thead><tr><th scope="col">When</th><th scope="col">What</th><th scope="col">Why</th><th scope="col">By</th><th scope="col">Note</th></tr></thead>
      <tbody>{tombstones.length ? tombstones.map((s) => (
        <tr key={s.id}>
          <td>{s.at.replace("T", " ").slice(0, 19)}</td>
          <td>{s.object_type} {s.object_id}{typeof s.details.title === "string" ? ` (${s.details.title})` : ""}</td>
          <td>{s.reason.replace("_", " ")}</td><td>{s.actor}</td>
          <td>{typeof s.details.note === "string" ? s.details.note : ""}</td>
        </tr>
      )) : <tr><td colSpan={5}>Nothing was deleted.</td></tr>}</tbody>
    </table>
  );
}

function Purge() {
  const [sha, setSha] = useState("");
  const [note, setNote] = useState("");
  const clean = sha.trim().toLowerCase();
  const find = useAction(async () => data(await api.GET("/api/v1/admin/files/{sha256}", { params: { path: { sha256: clean } } })), undefined, []);
  const purge = useAction(async () => data(await api.POST("/api/v1/admin/files/{sha256}/purge", {
    params: { path: { sha256: clean } }, body: { note },
  })), undefined, [operationsKey, ["admin-tombstones"]]);
  const found = find.data;
  return (
    <section aria-labelledby="purge-h">
      <h2 id="purge-h">Purge a file everywhere</h2>
      <p className="muted">For a leaked picture: every stored copy with this SHA-256, and every stored ZIP that contains it, is deleted. Backups keep theirs until they expire.</p>
      <form aria-label="Find a file" onSubmit={(e) => { e.preventDefault(); find.mutate(undefined); }}>
        <label>SHA-256 <input required pattern="[0-9a-fA-F]{64}" value={sha} onChange={(e) => setSha(e.target.value)} /></label>
        <button className="secondary" disabled={find.isPending}>Find</button>
      </form>
      <ErrorText error={find.error} />
      {found && <p>{found.artifacts.length} stored copies; inside {found.inside.length} stored ZIPs{found.inside.length ? `: ${found.inside.map((i) => `${i.kind} #${i.id}`).join(", ")}` : ""}.</p>}
      {found && (found.artifacts.length > 0 || found.inside.length > 0) && (
        <form aria-label="Purge the file" onSubmit={(e) => { e.preventDefault(); if (window.confirm("Delete every copy for good?")) purge.mutate(undefined); }}>
          <label>Why (kept in the audit history) <input required minLength={3} maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} /></label>
          <button className="destructive" disabled={purge.isPending}>Purge</button>
        </form>
      )}
      <ErrorText error={purge.error} />
      {purge.data && <p role="status">Purged: {purge.data.artifacts_deleted} copies and {purge.data.containers_deleted.length} ZIPs ({bytes(purge.data.freed_bytes)}).</p>}
    </section>
  );
}

function DeleteRecording() {
  const [id, setId] = useState("");
  const [reason, setReason] = useState("");
  const [packages, setPackages] = useState(false);
  const remove = useAction(async () => data(await api.POST("/api/v1/recordings/{recording_id}/deletion", {
    params: { path: { recording_id: id.trim() } }, body: { reason, purge_packages: packages },
  })), undefined, [operationsKey, ["admin-tombstones"]]);
  return (
    <section aria-labelledby="delete-h">
      <h2 id="delete-h">Delete a recording</h2>
      <p className="muted">At an owner's request: its steps, assignments, evidence links and uploaded bundle go; a tombstone stays.</p>
      <form aria-label="Delete a recording" onSubmit={(e) => { e.preventDefault(); if (window.confirm(`Delete recording ${id.trim()}?`)) remove.mutate(undefined); }}>
        <label>Recording id <input required value={id} onChange={(e) => setId(e.target.value)} /></label>
        <label>Reason <input required minLength={3} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} /></label>
        <label className="check"><input type="checkbox" checked={packages} onChange={(e) => setPackages(e.target.checked)} /> Also delete the frozen packages that include it</label>
        <button className="destructive" disabled={remove.isPending}>Delete recording</button>
      </form>
      <ErrorText error={remove.error} />
      {remove.data && <p role="status">Deleted {remove.data.recording_id}: {remove.data.uploads_deleted} uploads, {remove.data.packages_deleted.length} packages.</p>}
    </section>
  );
}
