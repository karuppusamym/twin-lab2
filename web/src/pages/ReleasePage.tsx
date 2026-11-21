import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { api, data, type Schemas } from "../api/client";
import { DeploymentBadge } from "../components/shared";
import { VerificationProgress, isActive } from "../components/Verification";
import { ActionButton, Badge, ErrorText, Notice, QueryView, useAction, useTitle } from "../components/ui";
import { capitalize, commitUrl, releaseName, short, when } from "../lib/format";
import { useCan } from "../session";
import { TombstoneTable } from "./OperationsPage";

export function ReleasePage() {
  const releaseId = Number(useParams().releaseId);
  const release = useQuery({
    queryKey: ["release", releaseId],
    queryFn: async () => data(await api.GET("/api/v1/releases/{release_id}", { params: { path: { release_id: releaseId } } })),
  });
  useTitle(release.data ? releaseName(release.data.app.name, release.data.release) : undefined);
  return <QueryView query={release}>{(r) => <ReleaseView detail={r} />}</QueryView>;
}

function ReleaseView({ detail }: { detail: Schemas["ReleaseDetail"] }) {
  const can = useCan();
  const { release, app, checks } = detail;
  const owner = can("owner", app.id);
  const builder = can("builder", app.id);
  const path = { params: { path: { release_id: release.id } } };
  const name = releaseName(app.name, release);
  const commit = commitUrl(checks.repository ?? release.repository, release.commit_sha);
  return (
    <>
      <p><Link to={`/apps/${app.id}`}>{app.name}</Link></p>
      <h1>{app.name} {release.label} <span className="muted heading-sha">({short(release.commit_sha, 7)})</span> <Badge kind={release.state}>{capitalize(release.state)}</Badge></h1>
      <Notice>{detail.notice}</Notice>
      <p className="muted">Packaged → Tested → Published. A release is Tested once its test results are recorded here, and only a Tested release can be published.</p>
      {(release.state === "published" || builder) && <p><a href={`/api/v1/releases/${release.id}/download`} aria-label={`Download the release ZIP of ${name}`}>Download release ZIP</a></p>}
      <dl className="release-details">
        <dt>Version</dt><dd>{release.label} <span className="muted">build {release.label.split(".")[1]} of package revision {release.label.split(".")[0]}</span></dd>
        <dt>Git commit</dt><dd>{commit ? <a href={commit}><code>{release.commit_sha}</code></a> : <code>{release.commit_sha}</code>}</dd>
        <dt>Build brief</dt><dd>{builder
          ? <a href={`/api/v1/apps/${app.id}/briefs/${release.revision}/download`} aria-label={`Download build brief ${short(release.revision)} of ${name}`}><code>{release.revision}</code></a>
          : <code>{release.revision}</code>}</dd>
        <dt>ZIP checksum</dt><dd><code>{release.archive_sha}</code></dd>
        {release.published_at && <><dt>Published</dt><dd><time dateTime={release.published_at}>{when(release.published_at)}</time></dd></>}
      </dl>

      <Deleted appId={app.id} />

      <section id="verification" aria-labelledby="verification-h">
        <h2 id="verification-h">Verification of commit <code>{short(release.commit_sha)}</code></h2>
        <p className="muted">Tested needs, on exactly this commit: every required check green{checks.windows.required && ", a green Windows validation check"}, and the scenario results, platform, launch instructions and reset check below.
          {checks.pr_number ? ` Built from pull request #${checks.pr_number}: a new push or force-push to it invalidates this verification.`
            : checks.source_ref ? ` Built from ${checks.source_ref}: a new push to it invalidates this verification.` : ""}</p>
        <dl className="release-details">
          <dt>GitHub repository</dt><dd>{checks.repository ? <code>{checks.repository}</code> : "Not linked to GitHub: no CI checks are read for this release."}</dd>
          <dt>Required checks</dt><dd>
            {checks.required.length ? checks.required.map((row) => (
              <span key={row.pattern}><Badge kind={row.state === "success" ? "pass" : "fail"}>{row.state}</Badge>{" "}
                {row.pattern === "*" ? "every check reported on this commit" : <code>{row.pattern}</code>}<br /></span>
            )) : "None configured. "}
            <span className="muted">Configured per application in {owner ? <Link to={`/apps/${app.id}/setup#verification`}>Setup / Advanced</Link> : "Setup / Advanced"}.</span></dd>
          <dt>Target platform</dt><dd>{checks.platform}</dd>
          {checks.windows.required && (
            <><dt>Windows validation</dt><dd>
              {checks.windows.state === "missing" ? <Badge kind="fail">Windows validation missing</Badge>
                : checks.windows.state === "success" ? <><Badge kind="pass">green</Badge> {checks.windows.checks.join(", ")}</>
                  : <><Badge kind="fail">not green</Badge> {checks.windows.checks.join(", ")}</>}
              {" "}<span className="muted">a check named like <code>{checks.windows.pattern}</code></span></dd></>
          )}
          <dt>Last checked</dt><dd>{checks.checked_at ?? "Never"}</dd>
        </dl>
        {checks.checks.length > 0 && (
          <table className="flow">
            <thead><tr><th scope="col">Check</th><th scope="col">Conclusion</th><th scope="col">Completed</th></tr></thead>
            <tbody>{checks.checks.map((ck) => (
              <tr key={`${ck.kind}/${ck.name}`}>
                <td>{ck.url ? <a href={ck.url}>{ck.name}</a> : ck.name} <span className="muted">{ck.kind === "status" ? "status" : "check run"}</span></td>
                <td><Badge kind={ck.conclusion === "success" ? "pass" : "fail"}>{ck.conclusion}</Badge></td>
                <td>{ck.completed_at ?? ""}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
        {checks.reasons.length > 0 ? (
          <><p><strong>Not ready for Tested:</strong></p><ul>{checks.reasons.map((r) => <li key={r}>{r}</li>)}</ul></>
        ) : checks.checked_at && <p><Badge kind="pass">CI green</Badge> on this commit.</p>}
        {release.state !== "published" && owner && (
          <>
            <span className="actions">
              <ActionButton secondary label="Check GitHub again" run={async () => data(await api.POST("/api/v1/releases/{release_id}/checks/refresh", path))} />
              <ActionButton label="Verify this build" disabled={isActive(detail.verification)}
                run={async () => data(await api.POST("/api/v1/releases/{release_id}/verify", path))} />
            </span>
            <p className="muted">Verify this build runs as a job: the worker reads the checks, replays the scenarios on each of the application's surfaces (web, mainframe, desktop), checks the business outcome and the reset against the deployment that reports this commit, stores the report and makes the release Tested when everything holds. Its progress shows here.</p>
          </>
        )}
        {detail.verification && <VerificationProgress key={detail.verification.id} initial={detail.verification} />}
        {builder && checks.reports.length > 0 && (
          <>
            <h3 id="reports-h">Verification reports <span className="muted">latest first</span></h3>
            <ul className="report-list" aria-labelledby="reports-h">{checks.reports.map((rep) => {
              const outcome = rep.tested === true ? "passed" : rep.tested === false ? "failed" : "outcome unknown";
              return (
                <li key={rep.id}>
                  <a href={`/api/v1/releases/${release.id}/reports/${rep.id}`}
                    aria-label={`Verification report ${rep.number} of ${name}${rep.latest ? ", the latest" : ""}: ${outcome}, ${when(rep.created_at)}${rep.requested_by ? ` by ${rep.requested_by}` : ""}`}>
                    Report {rep.number}</a>
                  {rep.latest && <Badge kind="known">Latest</Badge>}
                  <Badge kind={rep.tested === true ? "pass" : rep.tested === false ? "fail" : undefined}>{capitalize(outcome)}</Badge>
                  <span><time dateTime={rep.created_at}>{when(rep.created_at)}</time>{rep.requested_by ? ` by ${rep.requested_by}` : ""}</span>
                  <code className="muted" title={`SHA-256 ${rep.sha256}`}>{short(rep.sha256)}</code>
                </li>
              );
            })}</ul>
          </>
        )}
      </section>

      <section aria-labelledby="tests-h">
        <h2 id="tests-h">Test results</h2>
        {detail.test ? (
          <>
            <dl className="release-details">
              <dt>Tested commit</dt><dd><code>{detail.test.commit_sha}</code></dd>
              <dt>Supported platform</dt><dd>{detail.test.platform}</dd>
              <dt>Launch instructions</dt><dd className="notes">{detail.test.launch}</dd>
              <dt>Reset check</dt><dd><Badge kind={detail.test.reset_result}>{detail.test.reset_result}</Badge>{detail.test.reset_notes && ` ${detail.test.reset_notes}`}</dd>
              <dt>Tested by</dt><dd>{detail.test.tested_by}{detail.test.scope.role && ` (${detail.test.scope.role})`}, <time dateTime={detail.test.tested_at}>{when(detail.test.tested_at)}</time></dd>
              <dt>What was verified</dt><dd data-testid="verified-scope">{detail.test.scope.label}</dd>
              <dt>Deployment</dt><dd>{detail.test.scope.deployment_url ?? "No verified deployment"}</dd>
            </dl>
            <table className="flow">
              <thead><tr><th scope="col">Scenario</th><th scope="col">Result</th></tr></thead>
              <tbody>{detail.test.results.map((row) => <tr key={row.id ?? row.name}><td>{row.name}</td><td><Badge kind={row.result}>{row.result}</Badge></td></tr>)}</tbody>
            </table>
          </>
        ) : <p>Not tested yet: what this release does is unknown until its scenarios are run.</p>}
        {release.state !== "published" && owner && (detail.scenarios.length
          ? <TestForm detail={detail} />
          : <p>The build brief names no scenarios, so this release cannot be tested. Link a recording to the app and export a new brief.</p>)}
        {release.state === "tested" && owner && (
          <ActionButton label="Publish this release" confirm={`Publish ${name}?`} run={async () => data(await api.POST("/api/v1/releases/{release_id}/publish", path))} />
        )}
      </section>

      <section aria-labelledby="deployments-h">
        <h2 id="deployments-h">Deployments</h2>
        <p className="muted">A deployment is Verified when <code>GET &lt;URL&gt;/{detail.version_path}</code> reports this release's commit: it serves that commit, nothing more. What was tested is under Test results.</p>
        {detail.can_download && <p><a href={`/api/v1/releases/${release.id}/openshift.yaml`} aria-label={`OpenShift manifest of ${name}`}>OpenShift manifest (release)</a> <span className="muted">builds exactly commit <code>{short(release.commit_sha)}</code> and runs the built image by digest; its header says how to add the digest.</span></p>}
        {detail.deployments.map((d) => (
          <div key={d.environment}>
            <h3>{d.environment} <DeploymentBadge deployment={d} /></h3>
            <p><a href={d.url}>Open demo</a> <span className="muted">{d.reason}{d.checked_at && ` Checked ${d.checked_at}.`}</span></p>
            {builder && <ActionButton secondary label="Check version again" run={async () => data(await api.POST("/api/v1/releases/{release_id}/deployments/check", { ...path, body: { environment: d.environment } }))} />}
            {d.username && <p>Demo username <code>{d.username}</code></p>}
            {d.secret_ref && <p>Demo secret reference <code>{d.secret_ref}</code></p>}
          </div>
        ))}
        {owner && <DeploymentForm releaseId={release.id} />}
      </section>
    </>
  );
}

function TestForm({ detail }: { detail: Schemas["ReleaseDetail"] }) {
  const { release } = detail;
  const [commit, setCommit] = useState("");
  const [results, setResults] = useState<Record<string, Schemas["TestResult"] | "">>({});
  const [platform, setPlatform] = useState("");
  const [launch, setLaunch] = useState("");
  const [reset, setReset] = useState<"" | "pass" | "fail">("");
  const [resetNotes, setResetNotes] = useState("");
  const record = useAction(async () => data(await api.POST("/api/v1/releases/{release_id}/tests", {
    params: { path: { release_id: release.id } },
    body: {
      commit: commit.trim(), platform: platform.trim(), launch, reset_result: reset as "pass" | "fail", reset_notes: resetNotes.trim(),
      results: Object.fromEntries(Object.entries(results).filter(([, v]) => v)) as Record<string, Schemas["TestResult"]>,
    },
  })));
  const submit = (e: FormEvent) => { e.preventDefault(); record.mutate(undefined); };
  return (
    <form aria-label="Record test results" onSubmit={submit}>
      <label>Tested commit <input required placeholder={release.commit_sha} value={commit} onChange={(e) => setCommit(e.target.value)} /></label>
      <fieldset>
        <legend>Scenario results (from the build brief)</legend>
        {detail.scenarios.map((s) => (
          <label key={s.id}>{s.name}
            <select required value={results[s.id] ?? ""} onChange={(e) => setResults((r) => ({ ...r, [s.id]: e.target.value as Schemas["TestResult"] }))}>
              <option value="">Choose</option>
              {detail.results.map((v) => <option key={v}>{v}</option>)}
            </select>
          </label>
        ))}
      </fieldset>
      <label>Supported platform <input required placeholder="Windows 11, Python 3.13" value={platform} onChange={(e) => setPlatform(e.target.value)} /></label>
      <label>Launch instructions <textarea rows={4} required value={launch} onChange={(e) => setLaunch(e.target.value)} /></label>
      <label>Reset check
        <select required value={reset} onChange={(e) => setReset(e.target.value as "" | "pass" | "fail")}>
          <option value="">Choose</option><option>pass</option><option>fail</option>
        </select>
      </label>
      <label>Reset notes <input value={resetNotes} onChange={(e) => setResetNotes(e.target.value)} /></label>
      <ErrorText error={record.error} />
      <button disabled={record.isPending}>Record test results</button>
    </form>
  );
}

function DeploymentForm({ releaseId }: { releaseId: number }) {
  const [values, setValues] = useState({ environment: "", url: "", username: "", secret_ref: "" });
  const set = (k: keyof typeof values) => (e: { target: { value: string } }) => setValues((v) => ({ ...v, [k]: e.target.value }));
  const save = useAction(async () => data(await api.PUT("/api/v1/releases/{release_id}/deployments", {
    params: { path: { release_id: releaseId } },
    body: { environment: values.environment.trim(), url: values.url.trim(), username: values.username.trim(), secret_ref: values.secret_ref.trim() || undefined },
  })));
  return (
    <details className="advanced" id="deployment-setup">
      <summary>Setup / Advanced: deployment configuration</summary>
      <form aria-label="Deployment configuration" onSubmit={(e) => { e.preventDefault(); save.mutate(undefined); }}>
        <label>Environment <input required placeholder="OpenShift demo" value={values.environment} onChange={set("environment")} /></label>
        <label>Demo URL <input type="url" required value={values.url} onChange={set("url")} /></label>
        <label>Demo username <input value={values.username} onChange={set("username")} /></label>
        <label>Demo secret reference <input placeholder="vault:demo/billing" value={values.secret_ref} onChange={set("secret_ref")} /></label>
        <ErrorText error={save.error} />
        <button disabled={save.isPending}>Save deployment</button>
      </form>
    </details>
  );
}

/** What retention, an owner's request or a purge deleted from the application: the release still says so. */
function Deleted({ appId }: { appId: number }) {
  const stones = useQuery({
    queryKey: ["app-tombstones", appId],
    queryFn: async () => data(await api.GET("/api/v1/apps/{app_id}/tombstones", { params: { path: { app_id: appId } } })),
  });
  if (!stones.data?.tombstones.length) return null;
  return (
    <section id="deleted" aria-labelledby="deleted-h">
      <h2 id="deleted-h">Deleted since</h2>
      <p className="muted">Stored files of this application that were deleted. A download that needed one answers that it is gone.</p>
      <TombstoneTable tombstones={stones.data.tombstones} />
    </section>
  );
}
