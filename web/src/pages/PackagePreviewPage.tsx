import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, data, type Schemas } from "../api/client";
import { PackageFiles } from "../components/shared";
import { ErrorText, QueryView, useAction, useTitle } from "../components/ui";
import { plural, short } from "../lib/format";
import { useAppId } from "./AppPage";

/** The owner's preview of the draft package and the freeze (approval of exactly these bytes). */
export function PackagePreviewPage() {
  const appId = useAppId();
  const draft = useQuery({
    queryKey: ["app", appId, "draft"],
    queryFn: async () => data(await api.GET("/api/v1/apps/{app_id}/package-draft", { params: { path: { app_id: appId } } })),
  });
  useTitle(draft.data ? `Builder package preview · ${draft.data.app.name}` : "Builder package preview");
  return <QueryView query={draft}>{(d) => <Preview draft={d} />}</QueryView>;
}

function Preview({ draft }: { draft: Schemas["PackageDraft"] }) {
  const navigate = useNavigate();
  const [summary, setSummary] = useState("");
  const [approve, setApprove] = useState(false);
  const freeze = useAction(
    async () => data(await api.POST("/api/v1/apps/{app_id}/packages", {
      params: { path: { app_id: draft.app.id } },
      // the SHA-256 of exactly the draft shown here: the server refuses it if the draft changed
      body: { sha256: draft.sha256, summary, approve: true },
    })),
    (rev) => navigate(`/apps/${draft.app.id}/packages/${rev.rev}`),
  );
  const submit = (e: FormEvent) => { e.preventDefault(); if (approve) freeze.mutate(undefined); };
  return (
    <>
      <h1>Builder package preview: <Link to={`/apps/${draft.app.id}`}>{draft.app.name}</Link></h1>
      <p className="muted">This draft is what freezing stores and what the builder will get: {plural(draft.files.length, "file")},{" "}
        {draft.size} bytes, archive SHA-256 <code id="archive-sha256">{draft.sha256}</code>. Revision <code>{draft.revision}</code>.
        Nothing here is shared until you approve it.</p>

      <section id="report" aria-labelledby="report-h">
        <h2 id="report-h">Pre-freeze report</h2>
        <p id="report-summary"><strong>{draft.summary}</strong></p>
        {draft.report.length ? (
          <table className="flow">
            <thead><tr><th scope="col">Item</th><th scope="col">Outcome</th><th scope="col">Reasons</th></tr></thead>
            <tbody>{draft.report.map((entry) => (
              <tr key={entry.item} className={`report-${entry.outcome}`}><td>{entry.item}</td><td>{entry.outcome}</td><td>{entry.reasons.join("; ")}</td></tr>
            ))}</tbody>
          </table>
        ) : <p>Nothing was excluded or redacted.</p>}
      </section>

      <section id="approve" aria-labelledby="approve-h">
        <h2 id="approve-h">Approve for the builder</h2>
        {draft.unchanged && draft.latest ? (
          <p>Nothing changed since <Link to={`/apps/${draft.app.id}/packages/${draft.latest.rev}`}>revision {draft.latest.rev}</Link>, which is still the latest package.</p>
        ) : (
          <form onSubmit={submit} aria-label="Approve for the builder">
            <label>Change summary {draft.latest ? `(since revision ${draft.latest.rev})` : ""}
              <textarea rows={2} placeholder="What changed and why" value={summary} onChange={(e) => setSummary(e.target.value)} />
            </label>
            <label className="check">
              <input type="checkbox" required checked={approve} onChange={(e) => setApprove(e.target.checked)} />
              I reviewed this package and approve exactly these bytes (SHA-256 {short(draft.sha256)}…) for the builder.
            </label>
            <ErrorText error={freeze.error} />
            <button disabled={!approve || freeze.isPending}>Freeze as revision {draft.next_rev}</button>
          </form>
        )}
      </section>

      <section aria-label="Files">
        <PackageFiles files={draft.files} full />
      </section>
    </>
  );
}
