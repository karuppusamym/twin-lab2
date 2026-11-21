import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, data, type Schemas } from "../api/client";
import { CompletenessChecklist, PackageFiles } from "../components/shared";
import { ErrorText, QueryView, useAction, useTitle } from "../components/ui";
import { plural, short } from "../lib/format";
import { useAppId } from "./AppPage";

/** The owner's preview of the draft package and the freeze (approval of exactly these bytes). */
export function PackagePreviewPage() {
  const appId = useAppId();
  // the owner's reason to freeze over critical gaps: the draft is previewed with it, because the
  // package then carries it ("Known gaps") and the approval names exactly those bytes
  const [reason, setReason] = useState("");
  const draft = useQuery({
    queryKey: ["app", appId, "draft", reason],
    queryFn: async () => data(await api.GET("/api/v1/apps/{app_id}/package-draft", {
      params: { path: { app_id: appId }, query: reason ? { override_reason: reason } : {} },
    })),
  });
  useTitle(draft.data ? `Builder package preview · ${draft.data.app.name}` : "Builder package preview");
  return <QueryView query={draft}>{(d) => <Preview draft={d} reason={reason} onReason={setReason} />}</QueryView>;
}

function Preview({ draft, reason, onReason }: { draft: Schemas["PackageDraft"]; reason: string; onReason: (r: string) => void }) {
  const navigate = useNavigate();
  const [summary, setSummary] = useState("");
  const [approve, setApprove] = useState(false);
  const [written, setWritten] = useState(reason);
  const incomplete = !draft.completeness.passed;
  const withGaps = incomplete && draft.known_gaps != null;
  const freeze = useAction(
    async () => data(await api.POST("/api/v1/apps/{app_id}/packages", {
      params: { path: { app_id: draft.app.id } },
      // the SHA-256 of exactly the draft shown here: the server refuses it if the draft changed;
      // with critical gaps, only with the reason this draft was previewed with (audited, in the package)
      body: { sha256: draft.sha256, summary, approve: true, ...(withGaps ? { override_reason: reason } : {}) },
    })),
    (rev) => navigate(`/apps/${draft.app.id}/packages/${rev.rev}`),
  );
  const ready = approve && (!incomplete || withGaps);
  const submit = (e: FormEvent) => { e.preventDefault(); if (ready) freeze.mutate(undefined); };
  const addReason = (e: FormEvent) => { e.preventDefault(); setApprove(false); onReason(written.trim()); };
  return (
    <>
      <h1>Builder package preview: <Link to={`/apps/${draft.app.id}`}>{draft.app.name}</Link></h1>
      <p className="muted">This draft is what freezing stores and what the builder will get: {plural(draft.files.length, "file")},{" "}
        {draft.size} bytes, archive SHA-256 <code id="archive-sha256">{draft.sha256}</code>. Revision <code>{draft.revision}</code>.
        Nothing here is shared until you approve it.</p>

      <section id="completeness" aria-labelledby="completeness-h">
        <h2 id="completeness-h">Completeness</h2>
        <p className="muted">What a coding agent needs to rebuild this application so that its recorded scenarios replay against the copy
          (<a href="https://github.com/karuppusamym/twin-lab2/blob/main/docs/PACKAGE-COMPLETENESS.md">the spec</a>). A package with a critical
          item missing is frozen only with your written reason.</p>
        <CompletenessChecklist completeness={draft.completeness} fixLinks />
      </section>

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
          <>
          {incomplete && (
            <form onSubmit={addReason} aria-label="Known gaps" className="warning">
              <p>Critical items are missing. Add them (see Completeness above), or freeze the package anyway: write why, and the
                package tells the builder these gaps as "Known gaps". The reason is audited.</p>
              <label>Reason to freeze with known gaps (at least 10 characters)
                <textarea rows={2} required minLength={10} value={written} onChange={(e) => setWritten(e.target.value)} />
              </label>
              <button className="secondary" disabled={written.trim().length < 10 || written.trim() === reason}>
                {withGaps ? "Change the reason" : "Add the known gaps to the package"}</button>
              {withGaps && <p id="known-gaps-reason">This draft carries the known gaps with your reason: “{draft.known_gaps?.reason}”.</p>}
            </form>
          )}
          <form onSubmit={submit} aria-label="Approve for the builder">
            <label>Change summary {draft.latest ? `(since revision ${draft.latest.rev})` : ""}
              <textarea rows={2} placeholder="What changed and why" value={summary} onChange={(e) => setSummary(e.target.value)} />
            </label>
            <label className="check">
              <input type="checkbox" required checked={approve} onChange={(e) => setApprove(e.target.checked)} />
              I reviewed this package and approve exactly these bytes (SHA-256 {short(draft.sha256)}…) for the builder.
            </label>
            <ErrorText error={freeze.error} />
            <button disabled={!ready || freeze.isPending}>{incomplete ? "Freeze with known gaps" : "Freeze"} as revision {draft.next_rev}</button>
          </form>
          </>
        )}
      </section>

      <section aria-label="Files">
        <PackageFiles files={draft.files} full />
      </section>
    </>
  );
}
