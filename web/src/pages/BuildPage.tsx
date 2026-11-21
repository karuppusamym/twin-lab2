import { useQuery } from "@tanstack/react-query";
import { useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, data, newRequestKey, type Schemas } from "../api/client";
import { PackageFiles } from "../components/shared";
import { Badge, Crumbs, ErrorText, Notice, QueryView, useAction, useTitle } from "../components/ui";
import { amount, plural, releaseName, short, when } from "../lib/format";
import { useCan } from "../session";
import { useAppId } from "./AppPage";

export function BuildPage() {
  const appId = useAppId();
  const page = useQuery({
    queryKey: ["app", appId, "builds"],
    queryFn: async () => data(await api.GET("/api/v1/apps/{app_id}/builds", { params: { path: { app_id: appId } } })),
  });
  useTitle(page.data ? `Build · ${page.data.app.name}` : undefined);
  return <QueryView query={page}>{(p) => <BuildView page={p} />}</QueryView>;
}

function BuildView({ page }: { page: Schemas["BuildPage"] }) {
  const can = useCan();
  const owner = can("owner", page.app.id);
  const { app, pack } = page;
  const detail = useQuery({
    queryKey: ["app", app.id],
    queryFn: async () => data(await api.GET("/api/v1/apps/{app_id}", { params: { path: { app_id: app.id } } })),
  });
  const gitUrl = detail.data?.git_url ?? null;
  return (
    <>
      <Crumbs><Link to={`/apps/${app.id}`}>{app.name}</Link> · Build</Crumbs>
      <h1>Build sessions: {app.name}</h1>
      <p className="muted">A build session is one provider's attempt at one approved package revision, in its own branch and
        workspace, with its own conversation, budget and results. One revision can have many sessions; compare their
        commits with the same acceptance checks before you choose one. Publishing a release stays one at a time.</p>
      {page.problem || !pack ? (
        <section><p className="error">{page.problem ?? "No builder-approved package yet."}</p>
          {owner && <p><Link to={`/apps/${app.id}/build-pack/preview#approve`}>Preview and approve it</Link>.</p>}</section>
      ) : (
        <>
          <section id="pack" aria-labelledby="pack-h">
            <h2 id="pack-h">1. The approved package</h2>
            {page.frozen ? (
              <p><Badge kind="verified">Approved</Badge> revision {pack.rev} by {page.frozen.frozen_by} at {when(page.frozen.frozen_at, 19)} UTC.
                A session sends exactly the bytes of the revision it is given.{" "}
                {owner && <Link to={`/apps/${app.id}/build-pack/preview`}>Preview and approve a newer package</Link>}</p>
            ) : (
              <p>A session sends only a package an owner approved.{" "}
                {owner && <Link to={`/apps/${app.id}/build-pack/preview#approve`}>Preview and approve it</Link>}</p>
            )}
            <p>This is exactly what the pack contains: {plural(pack.files.length, "file")}, {pack.size} bytes, archive SHA-256{" "}
              <code id="archive-sha256">{pack.sha256}</code>. Revision <code>{pack.revision}</code>.</p>
            <p><a href={`/api/v1/apps/${app.id}/briefs/${pack.revision}/download`} aria-label={`Download the build pack ${short(pack.revision)} of ${app.name}`}>Download this build pack</a></p>
            <PackageFiles files={pack.files} full />
          </section>
          <StartSession page={page} pack={pack} gitUrl={gitUrl} />
        </>
      )}
      {page.sessions.length > 0 && <Sessions page={page} />}
      {pack && page.frozen && <RegisterRelease page={page} gitUrl={gitUrl} />}
    </>
  );
}

/** Why a session cannot start yet, with where to fix it ("" = it can). */
function startBlocked(page: Schemas["BuildPage"], gitUrl: string | null, owner: boolean, admin: boolean) {
  if (!page.frozen) return { reason: "Freeze the pack first.", href: `/apps/${page.app.id}/build-pack/preview#approve`, fix: owner ? "Preview and approve it" : "" };
  if (gitUrl === "") return { reason: "No demo repository is set for this application.", href: `/apps/${page.app.id}/setup#git`, fix: owner ? "Connect one" : "" };
  if (page.repos.length === 0) return { reason: "No GitHub repository is connected for build sessions.", href: "/setup/builds#repos", fix: admin ? "Connect one" : "" };
  if (page.providers.length === 0) return { reason: "No build provider is ready.", href: "/setup/builds", fix: admin ? "Set one up" : "" };
  return null;
}

function StartSession({ page, pack, gitUrl }: { page: Schemas["BuildPage"]; pack: Schemas["ApprovedPack"]; gitUrl: string | null }) {
  const can = useCan();
  const blocked = startBlocked(page, gitUrl, can("owner", page.app.id), can("admin"));
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const parent = Number(params.get("parent")) || null;
  const requestKey = useMemo(() => newRequestKey(), []);
  const firstReady = page.providers.find((p) => !p.ready) ?? page.providers[0];
  const [provider, setProvider] = useState(firstReady?.name ?? "");
  const chosen = page.providers.find((p) => p.name === provider);
  const [model, setModel] = useState("");
  const [revision, setRevision] = useState(page.revisions[0]?.revision ?? pack.revision);
  const [repo, setRepo] = useState(page.repos[0]?.repo ?? "");
  const [baseBranch, setBaseBranch] = useState("");
  const [start, setStart] = useState(parent ? String(parent) : "");
  const [summary, setSummary] = useState("");
  const [budget, setBudget] = useState(amount(chosen?.budget ?? page.default_budget));
  const changedPack = page.previous !== null && page.previous.revision !== pack.revision;
  const begin = useAction(async () => data(await api.POST("/api/v1/apps/{app_id}/builds", {
    params: { path: { app_id: page.app.id } },
    body: {
      provider, model: model.trim() || undefined, revision, repo, base_branch: baseBranch.trim() || undefined,
      start_from_build: start ? Number(start) : undefined, change_summary: summary, budget: Number(budget), request_key: requestKey,
    },
  })), (started) => navigate(`/builds/${started.build.id}`, { state: { notice: started.notice } }));
  const submit = (e: FormEvent) => { e.preventDefault(); begin.mutate(undefined); };
  return (
    <section id="start" aria-labelledby="start-h">
      <h2 id="start-h">2. New build session</h2>
      {blocked && (
        <p className="blocked" id="start-blocked" role="note"><b>Cannot start yet:</b> {blocked.reason}{" "}
          {blocked.fix ? <Link to={blocked.href}>{blocked.fix} →</Link> : <span className="muted">({blocked.href.startsWith("/setup") ? "a portal admin" : "an owner"} does this)</span>}</p>
      )}
      {changedPack && page.previous && (
        <>
          <p>The previous session (#{page.previous.id}) used revision <code>{short(page.previous.revision)}</code>
            {page.previous.pr_number && <> and opened <a href={page.previous.pr_url ?? undefined}>pull request #{page.previous.pr_number}</a></>}.
            The provider gets this pack, that revision and your summary of what changed.</p>
          {page.changes.length > 0 && <ul className="muted">{page.changes.map((c) => <li key={c}>{c}</li>)}</ul>}
        </>
      )}
      <form id="build-form" aria-label="New build session" onSubmit={submit}>
        <label>Provider
          <select value={provider} onChange={(e) => { setProvider(e.target.value); const p = page.providers.find((x) => x.name === e.target.value); if (p) setBudget(amount(p.budget)); }}>
            {page.providers.map((p) => <option key={p.name} value={p.name} disabled={Boolean(p.ready)}>{p.label}{p.ready ? " (not ready: admins only see this)" : ""}</option>)}
          </select>
        </label>
        <ul className="muted" id="provider-notes">
          {page.providers.map((p) => (
            <li key={p.name}><strong>{p.label}</strong>: {p.capability_text || "no capabilities"}. Budget in {p.budget_unit}{p.model ? `; model ${p.model}` : ""}.
              {p.ready && <> <span className="error">{p.ready}</span></>}</li>
          ))}
        </ul>
        <label>Model <input placeholder="the provider's configured model" value={model} onChange={(e) => setModel(e.target.value)} /></label>
        <label>Package revision
          <select value={revision} onChange={(e) => setRevision(e.target.value)}>
            {page.revisions.map((r) => <option key={r.rev} value={r.revision}>Revision {r.rev} · {short(r.sha256)} · approved by {r.approved_by}</option>)}
          </select>
        </label>
        <label>Repository
          <select required value={repo} onChange={(e) => setRepo(e.target.value)}>
            {page.repos.map((r) => <option key={r.repo} value={r.repo}>{r.repo}</option>)}
          </select>
        </label>
        <label>Base branch <input value={baseBranch} onChange={(e) => setBaseBranch(e.target.value)} placeholder={`${page.repos.find((r) => r.repo === repo)?.default_branch ?? "main"} (the repository's default)`} /></label>
        <label>Starting commit
          <select value={start} onChange={(e) => setStart(e.target.value)}>
            <option value="">The base branch's head when the session is queued</option>
            {page.starting_points.filter((b) => b.head_sha).map((b) => (
              <option key={b.id} value={b.id}>Session #{b.id} ({b.provider_label}): {short(b.head_sha)}</option>
            ))}
          </select>
        </label>
        <p className="muted">Each session works on its own branch <code>twinlab2/app-{page.app.id}/session-N-provider</code> and, where the provider runs here, its own workspace.</p>
        <label>What changed since the previous build
          <textarea rows={4} required={changedPack} value={summary} onChange={(e) => setSummary(e.target.value)}
            placeholder={page.previous ? "Required for a later build of a changed pack" : "Optional for the first build"} />
        </label>
        <label>Budget <input type="number" min="0.01" step="any" required value={budget} onChange={(e) => setBudget(e.target.value)} /></label>
        <p className="muted">In the provider's own unit (Devin: ACUs; Claude: US dollars; Copilot: premium requests).</p>
        {chosen?.ready && <Notice>{chosen.ready}</Notice>}
        <ErrorText error={begin.error} />
        <button disabled={Boolean(blocked) || Boolean(chosen?.ready) || begin.isPending} aria-describedby={blocked ? "start-blocked" : undefined}>Start build session</button>
        {blocked && <span className="muted">{blocked.reason}</span>}
      </form>
    </section>
  );
}

/** A release of a commit built outside a session, or of a session's head (replaces the README's release command). */
function RegisterRelease({ page, gitUrl }: { page: Schemas["BuildPage"]; gitUrl: string | null }) {
  const navigate = useNavigate();
  const [sha, setSha] = useState("");
  const [repo, setRepo] = useState("");
  const ready = page.sessions.filter((b) => b.head_sha);
  const register = useAction(async (body: Schemas["ReleaseInput"]) => data(await api.POST("/api/v1/apps/{app_id}/releases", {
    params: { path: { app_id: page.app.id } }, body,
  })), (r) => navigate(r.href, { state: { notice: r.created ? `Registered release ${releaseName(null, r.release)}. ${r.next}` : `Commit ${short(r.release.commit_sha)} is already release ${releaseName(null, r.release)}. ${r.next}` } }),
  [["app", page.app.id], ["apps"], ["home"]]);
  return (
    <section id="register" aria-labelledby="register-h">
      <h2 id="register-h">Register a release</h2>
      <p className="muted">A release is the commit to verify and publish. A build session&apos;s commit is registered when you verify it from the session&apos;s page; here you can register it without verifying, or register a commit built elsewhere.</p>
      {ready.length > 0 && (
        <ul className="list">{ready.map((b) => (
          <li key={b.id}>Session #{b.id} ({b.provider_label}): <code>{short(b.head_sha)}</code>{" "}
            <button type="button" className="secondary" disabled={register.isPending} onClick={() => register.mutate({ build_id: b.id })}
              aria-label={`Register the commit of session ${b.id} as a release`}>Register as a release</button>{" "}
            <Link to={`/builds/${b.id}`}>Verify this build</Link></li>
        ))}</ul>
      )}
      <form aria-label="Register a commit" onSubmit={(e) => { e.preventDefault(); register.mutate({ sha: sha.trim(), repo: repo.trim() || undefined }); }}>
        <label>Commit SHA (40 characters) <input required pattern="[0-9a-fA-F]{40}" value={sha} onChange={(e) => setSha(e.target.value)} /></label>
        <label>Repository <input value={repo} placeholder={gitUrl ? "the demo repository" : "owner/name"} onChange={(e) => setRepo(e.target.value)} /></label>
        <ErrorText error={register.error} />
        <button className="secondary" disabled={register.isPending}>Register the commit</button>
      </form>
    </section>
  );
}

function Sessions({ page }: { page: Schemas["BuildPage"] }) {
  const navigate = useNavigate();
  const [chosen, setChosen] = useState<number[]>([]);
  const toggle = (id: number) => setChosen((c) => (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]));
  return (
    <section id="history" aria-labelledby="history-h">
      <h2 id="history-h">Sessions</h2>
      <form id="compare-form" aria-label="Compare sessions" onSubmit={(e) => {
        e.preventDefault();
        navigate(`/apps/${page.app.id}/sessions/compare${chosen.length ? `?${chosen.map((id) => `session=${id}`).join("&")}` : ""}`);
      }}>
        <div className="scroll">
          <table className="flow">
            <thead><tr><th scope="col"><span className="visually-hidden">Compare</span></th><th scope="col">Session</th><th scope="col">Provider</th><th scope="col">State</th><th scope="col">Revision</th><th scope="col">Branch</th><th scope="col">Cost</th><th scope="col">Questions</th><th scope="col">Pull request</th></tr></thead>
            <tbody>{page.sessions.map((b) => (
              <tr key={b.id}>
                <td>{b.head_sha && <input type="checkbox" aria-label={`Compare session ${b.id}`} checked={chosen.includes(b.id)} onChange={() => toggle(b.id)} />}</td>
                <td><Link to={`/builds/${b.id}`}>#{b.id}</Link>{b.parent_build && <span className="muted"> from #{b.parent_build}</span>}</td>
                <td>{b.provider_label}{b.model && <span className="muted"> {b.model}</span>}</td>
                <td><Badge kind={b.state}>{b.state_label}</Badge></td>
                <td><code>{short(b.revision)}</code>{b.package_rev && ` (rev ${b.package_rev})`}</td>
                <td><code>{b.branch}</code></td>
                <td>{b.cost !== null && `${amount(b.cost)} ${b.cost_unit}`} <span className="muted">/ {amount(b.budget)} {b.budget_unit}</span></td>
                <td>{b.outstanding > 0 && <strong>{b.outstanding} outstanding</strong>}</td>
                <td>{b.pr_number && <a href={b.pr_url ?? undefined}>#{b.pr_number}</a>}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
        <button className="secondary">Compare results</button>
      </form>
    </section>
  );
}
