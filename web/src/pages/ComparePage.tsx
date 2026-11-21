import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api, data, type Schemas } from "../api/client";
import { Badge, Crumbs, ErrorText, QueryView, useAction, useTitle } from "../components/ui";
import { amount, capitalize, short } from "../lib/format";
import { useCan } from "../session";
import { useAppId } from "./AppPage";

export function ComparePage() {
  const appId = useAppId();
  const [params] = useSearchParams();
  const sessions = params.getAll("session").map(Number).filter(Boolean);
  const comparison = useQuery({
    queryKey: ["app", appId, "compare", sessions],
    queryFn: async () => data(await api.GET("/api/v1/apps/{app_id}/builds/compare", {
      params: { path: { app_id: appId }, query: sessions.length ? { session: sessions } : {} },
    })),
  });
  useTitle(comparison.data ? `Compare results · ${comparison.data.app.name}` : undefined);
  return <QueryView query={comparison}>{(c) => <CompareView comparison={c} />}</QueryView>;
}

function CompareView({ comparison }: { comparison: Schemas["Comparison"] }) {
  const can = useCan();
  const { app, results } = comparison;
  const run = useAction(async () => data(await api.POST("/api/v1/apps/{app_id}/builds/compare", {
    params: { path: { app_id: app.id } }, body: { sessions: results.map((r) => r.build.id) },
  })));
  const errors = run.data?.errors ?? comparison.errors ?? [];
  return (
    <>
      <Crumbs><Link to={`/apps/${app.id}`}>{app.name}</Link> · <Link to={`/apps/${app.id}/build`}>Build</Link> · Compare results</Crumbs>
      <h1>Compare results: {app.name}</h1>
      <p className="muted">Each candidate's commit is checked with the same acceptance checks as a release: the commit's
        required GitHub checks and TwinLab2's independent scenario checks against a deployment of that commit. Nothing is
        merged or published from here.</p>
      {results.length === 0 ? <p>No session has produced a commit yet.</p> : (
        <>
          <div className="scroll">
            <table className="flow" id="comparison">
              <thead><tr><th scope="col">Session</th><th scope="col">Provider</th><th scope="col">Revision</th><th scope="col">Commit</th><th scope="col">Cost</th><th scope="col">Checks</th><th scope="col">Verification</th><th scope="col">Scenarios</th></tr></thead>
              <tbody>{results.map((r) => {
                const b = r.build;
                return (
                  <tr key={b.id} id={`candidate-${b.id}`}>
                    <td><Link to={`/builds/${b.id}`}>#{b.id}</Link></td>
                    <td>{b.provider_label}{b.model && <span className="muted"> {b.model}</span>}</td>
                    <td><code>{short(b.revision)}</code>{b.package_rev && ` (rev ${b.package_rev})`}</td>
                    <td>{b.head_sha ? <><code>{short(b.head_sha)}</code>{b.pr_number && <> · <a href={b.pr_url ?? undefined}>#{b.pr_number}</a></>}</> : "none"}</td>
                    <td>{b.cost !== null && `${amount(b.cost)} ${b.cost_unit}`}</td>
                    <td>{r.checks.length ? r.checks.map((c) => <Badge key={c.name} kind={c.conclusion}>{c.name}: {c.conclusion}</Badge>) : <span className="muted">not fetched</span>}</td>
                    <td>{r.release ? (
                      <><Link to={`/releases/${r.release.id}`}>release {r.release.version}</Link>: <Badge kind={r.release.state}>{capitalize(r.release.state)}</Badge>
                        {r.reasons.map((reason) => <span key={reason}><br /><span className="muted">{reason}</span></span>)}</>
                    ) : <span className="muted">not verified yet</span>}</td>
                    <td>{r.scenarios.length ? r.scenarios.map((s) => <span key={s.name}>{s.name}: {s.result}<br /></span>) : <span className="muted">none recorded</span>}</td>
                  </tr>
                );
              })}</tbody>
            </table>
          </div>
          {errors.map((e) => <p key={e.build_id} className="error">Session #{e.build_id}: {e.detail}</p>)}
          {can("owner", app.id) ? (
            <>
              <ErrorText error={run.error} />
              <button type="button" disabled={run.isPending} onClick={() => run.mutate(undefined)}>Run the acceptance checks on every candidate</button>
            </>
          ) : <p className="muted">An owner runs the checks.</p>}
        </>
      )}
    </>
  );
}
