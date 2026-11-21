import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, data } from "../api/client";
import { CompletenessChecklist, PackageFiles } from "../components/shared";
import { Crumbs, QueryView, useTitle } from "../components/ui";
import { useAppId } from "./AppPage";

export function PackagePage() {
  const appId = useAppId();
  const rev = Number(useParams().rev);
  const pkg = useQuery({
    queryKey: ["app", appId, "package", rev],
    queryFn: async () => data(await api.GET("/api/v1/apps/{app_id}/packages/{rev}", { params: { path: { app_id: appId, rev } } })),
  });
  useTitle(pkg.data ? `Package revision ${rev} · ${pkg.data.app.name}` : undefined);
  return (
    <QueryView query={pkg}>
      {(p) => (
        <>
          <Crumbs><Link to={`/apps/${appId}`}>{p.app.name}</Link></Crumbs>
          <h1>Builder package revision {p.rev}</h1>
          <p><a className="button" href={`/api/v1/apps/${appId}/packages/${p.rev}/download`}>Download revision {p.rev}</a></p>
          <dl className="release-details">
            <dt>Approved by</dt><dd id="approved-by">{p.approved_by}</dd>
            <dt>Approved at</dt><dd>{p.approved_at}</dd>
            <dt>SHA-256</dt><dd><code id="package-sha256">{p.sha256}</code></dd>
            <dt>Content revision</dt><dd><code>{p.revision}</code></dd>
            <dt>Size</dt><dd>{p.size} bytes</dd>
          </dl>
          <p className="muted">A frozen revision never changes: a change to the evidence or scenarios is frozen as a new revision.</p>
          <section aria-labelledby="summary-h">
            <h2 id="summary-h">Change summary</h2>
            <pre className="notes">{p.summary}</pre>
          </section>
          {p.known_gaps && (
            <section aria-labelledby="known-gaps-h" className="warning" id="known-gaps">
              <h2 id="known-gaps-h">Known gaps</h2>
              <p>Frozen with {p.known_gaps.gaps.length} critical item{p.known_gaps.gaps.length === 1 ? "" : "s"} missing, for this reason:</p>
              <blockquote>{p.known_gaps.reason}</blockquote>
              <ul>{p.known_gaps.gaps.map((g) => <li key={g.item}>{g.title}</li>)}</ul>
            </section>
          )}
          {p.completeness && (
            <section aria-labelledby="completeness-h">
              <h2 id="completeness-h">Completeness</h2>
              <CompletenessChecklist completeness={p.completeness} />
            </section>
          )}
          <section aria-labelledby="files-h">
            <h2 id="files-h">Files</h2>
            <PackageFiles files={p.files} />
          </section>
          {p.report && (
            <section aria-labelledby="report-h">
              <h2 id="report-h">Pre-freeze report</h2>
              {p.report.length
                ? p.report.map((entry) => <p key={entry.item}>{entry.item}: {entry.outcome} — {entry.reasons.join("; ")}</p>)
                : <p>Nothing was excluded or redacted.</p>}
            </section>
          )}
          {p.diff && (
            <section aria-labelledby="diff-h">
              <h2 id="diff-h">Diff against revision {p.rev - 1}</h2>
              <pre className="file">{p.diff}</pre>
            </section>
          )}
        </>
      )}
    </QueryView>
  );
}
