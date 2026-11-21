import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, data, type Schemas } from "../api/client";
import { ConflictView, Crumbs, DirtyNote, ErrorText, QueryView, conflictOf, useAction, useDraft, useField, useLeaveWarning, useTitle } from "../components/ui";
import { short } from "../lib/format";
import { useAppId } from "./AppPage";

export function AppSetupPage() {
  const appId = useAppId();
  const setup = useQuery({
    queryKey: ["app", appId, "setup"],
    queryFn: async () => data(await api.GET("/api/v1/apps/{app_id}/setup", { params: { path: { app_id: appId } } })),
  });
  useTitle(setup.data ? `${setup.data.app.name} setup` : undefined);
  return <QueryView query={setup}>{(s) => <SetupView setup={s} />}</QueryView>;
}

function SetupView({ setup }: { setup: Schemas["AppSetup"] }) {
  const app = setup.app;
  return (
    <>
      <Crumbs><Link to="/setup">Setup / Advanced</Link> · <Link to={`/apps/${app.id}`}>{app.name}</Link></Crumbs>
      <h1>{app.name}: Setup / Advanced</h1>
      <p>Technical details the main flow does not need: how the recorder recognises the application, where its demo source lives, and where its releases are deployed.</p>
      <div className="columns">
        <Identities setup={setup} />
        <section id="git" aria-labelledby="git-h">
          <h2 id="git-h">Git repository</h2>
          <GitForm appId={app.id} gitUrl={app.git_url} revision={app.revision} />
        </section>
      </div>
      <DemoTarget setup={setup} />
      <section id="verification" aria-labelledby="verification-h">
        <h2 id="verification-h">Release verification</h2>
        <p className="muted">What a release of this application needs before it is Tested. GitHub repository:{" "}
          {setup.github_repo ? <code>{setup.github_repo}</code> : "none (set a github.com Demo repository above)"}.
          GitHub token: {setup.github_token_configured ? "configured" : "not configured (set TWINLAB2_GITHUB_TOKEN)"}.</p>
        <VerificationForm setup={setup} />
        <h3>Verify a commit</h3>
        <VerifyCommit appId={app.id} repo={setup.github_repo ?? ""} />
      </section>
      <section id="deployments" aria-labelledby="deployments-h">
        <h2 id="deployments-h">Deployment configuration</h2>
        {setup.releases.length ? setup.releases.map((r) => (
          <p key={r.id}><Link to={`/releases/${r.id}#deployment-setup`}>{r.version}</Link> <code>{short(r.commit_sha)}</code></p>
        )) : <p>No release yet.</p>}
      </section>
    </>
  );
}

function Identities({ setup }: { setup: Schemas["AppSetup"] }) {
  const appId = setup.app.id;
  const [key, setKey] = useState("");
  const [match, setMatch, matchDraft] = useField(setup.app.url_match, setup.app.revision);
  useLeaveWarning(matchDraft.dirty);
  const add = useAction(async () => data(await api.POST("/api/v1/apps/{app_id}/identities", { params: { path: { app_id: appId } }, body: { key: key.trim() } })), () => setKey(""));
  const saveMatch = useAction(async (revision: number) => data(await api.PUT("/api/v1/apps/{app_id}/url-match", { params: { path: { app_id: appId } }, body: { url_match: match as "path" | "host", expected_revision: revision } })),
    (s) => matchDraft.reset(s.app.url_match, s.app.revision));
  const matchTheirs = conflictOf<Schemas["AppSetup"]>(saveMatch.error);
  return (
    <section id="identity" aria-labelledby="identity-h">
      <h2 id="identity-h">Identities</h2>
      <p className="muted">A recording can target this application only once it has an identity.</p>
      {setup.identities.length
        ? setup.identities.map((i) => <p key={i.key}><code>{i.key}</code></p>)
        : <p>No identity yet: a recording cannot target this application until it has one.</p>}
      <form aria-label="Add identity" onSubmit={(e) => { e.preventDefault(); add.mutate(undefined); }}>
        <label>Executable name, web address or mainframe host{" "}
          <input required value={key} onChange={(e) => setKey(e.target.value)} placeholder="CaseDesk.exe, https://billing.example.test or tn3270://mainframe.example.test:23" />
        </label>
        <ErrorText error={add.error} />
        <button disabled={add.isPending}>Add identity</button>
      </form>
      {setup.identities.some((i) => i.surface === "web") && (
        <form aria-label="Web address matching" onSubmit={(e) => { e.preventDefault(); saveMatch.mutate(matchDraft.revision); }}>
          <label>Web addresses match
            <select value={match} onChange={(e) => setMatch(e.target.value as "path" | "host")}>
              <option value="path">by path (the longest matching path wins)</option>
              <option value="host">the whole host</option>
            </select>
          </label>
          <DirtyNote dirty={matchDraft.dirty} />
          {matchTheirs ? (
            <ConflictView busy={saveMatch.isPending} rows={[{ label: "Web addresses match", mine: match, theirs: matchTheirs.app.url_match }]}
              onKeepMine={() => saveMatch.mutate(matchTheirs.app.revision)}
              onTakeTheirs={() => { matchDraft.reset(matchTheirs.app.url_match, matchTheirs.app.revision); saveMatch.reset(); }} />
          ) : <ErrorText error={saveMatch.error} />}
          <button className="secondary" disabled={saveMatch.isPending}>Save matching</button>
        </form>
      )}
    </section>
  );
}

const ENGINE_LABELS: Record<string, string> = { sqlite: "SQLite", postgresql: "PostgreSQL", sqlserver: "SQL Server" };

function DemoTarget({ setup }: { setup: Schemas["AppSetup"] }) {
  const appId = setup.app.id;
  const [backend, setBackend, backendDraft] = useField(setup.app.data_backend, setup.app.revision);
  useLeaveWarning(backendDraft.dirty);
  const save = useAction(async (revision: number) => data(await api.PUT("/api/v1/apps/{app_id}/data-backend", {
    params: { path: { app_id: appId } },
    body: { data_backend: backend as Schemas["DataBackendInput"]["data_backend"], expected_revision: revision },
  })), (s) => backendDraft.reset(s.app.data_backend, s.app.revision));
  const theirs = conflictOf<Schemas["AppSetup"]>(save.error);
  const backendLabel = (v: string) => setup.data_backends.find((c) => c.value === v)?.label ?? v;
  const b = setup.data_backend;
  return (
    <section id="demo-target" aria-labelledby="demo-target-h">
      <h2 id="demo-target-h">Data and deployment</h2>
      <p>The build pack asks for <strong>{ENGINE_LABELS[b.engine]}</strong> ({b.source}): {b.reason}</p>
      {b.tables.length > 0
        ? <p className="muted">Tables in the data model: {b.tables.map((t) => <code key={t}>{t} </code>)}</p>
        : <p className="muted">No data model: attach the DDL as a <em>Database</em> attachment (.sql), or the builder infers the tables from the recordings.</p>}
      <form aria-label="Data backend" onSubmit={(e) => { e.preventDefault(); save.mutate(backendDraft.revision); }}>
        <label>Data backend
          <select value={backend} onChange={(e) => setBackend(e.target.value as typeof backend)}>
            {setup.data_backends.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
          </select>
        </label>
        <DirtyNote dirty={backendDraft.dirty} />
        {theirs ? (
          <ConflictView busy={save.isPending} rows={[{ label: "Data backend", mine: backendLabel(backend), theirs: backendLabel(theirs.app.data_backend) }]}
            onKeepMine={() => save.mutate(theirs.app.revision)}
            onTakeTheirs={() => { backendDraft.reset(theirs.app.data_backend, theirs.app.revision); save.reset(); }} />
        ) : <ErrorText error={save.error} />}
        <button className="secondary" disabled={save.isPending}>Save data backend</button>
      </form>
      {setup.deployment.kind === "openshift"
        ? <p>Deployed on OpenShift from the demo repository: the pack carries {setup.deployment.files.map((f) => <code key={f}>{f} </code>)}for the builder to commit; pods are then set up by hand with <code>oc apply</code>.</p>
        : <p>A desktop demo: built and installed on a local machine or VM, with no OpenShift files.</p>}
    </section>
  );
}

function GitForm({ appId, gitUrl, revision }: { appId: number; gitUrl: string; revision: number }) {
  const [url, setUrl, urlDraft] = useField(gitUrl, revision);
  useLeaveWarning(urlDraft.dirty);
  const save = useAction(async (rev: number) => data(await api.PATCH("/api/v1/apps/{app_id}", { params: { path: { app_id: appId } }, body: { git_url: url.trim(), expected_revision: rev } })),
    (a) => urlDraft.reset(a.git_url, a.revision));
  const theirs = conflictOf<Schemas["AppDetail"]>(save.error);
  return (
    <form aria-label="Git repository" onSubmit={(e) => { e.preventDefault(); save.mutate(urlDraft.revision); }}>
      <label>Demo repository <DirtyNote dirty={urlDraft.dirty} /> <input maxLength={500} value={url} onChange={(e) => setUrl(e.target.value)} /></label>
      {theirs ? (
        <ConflictView busy={save.isPending} rows={[{ label: "Demo repository", mine: url.trim(), theirs: theirs.git_url }]}
          onKeepMine={() => save.mutate(theirs.revision)}
          onTakeTheirs={() => { urlDraft.reset(theirs.git_url, theirs.revision); save.reset(); }} />
      ) : <ErrorText error={save.error} />}
      <button disabled={save.isPending}>Save repository</button>
    </form>
  );
}

function VerificationForm({ setup }: { setup: Schemas["AppSetup"] }) {
  const app = setup.app;
  const settingsOf = (a: Schemas["AppSetup"]["app"]) => ({
    required_checks: a.required_checks, target_platform: a.target_platform,
    windows_check_pattern: a.windows_check_pattern, lab_url: a.lab_url, launch: a.launch,
  });
  const draft = useDraft(settingsOf(app), app.revision);
  useLeaveWarning(draft.dirty);
  const values = draft.value;
  const set = (k: keyof typeof values) => (e: { target: { value: string } }) => { const value = e.target.value; draft.set((v) => ({ ...v, [k]: value })); };
  const save = useAction(async (revision: number) => data(await api.PUT("/api/v1/apps/{app_id}/verification-settings", {
    params: { path: { app_id: app.id } },
    body: { ...values, target_platform: values.target_platform as Schemas["VerificationSettings"]["target_platform"], expected_revision: revision },
  })), (s) => draft.reset(settingsOf(s.app), s.app.revision));
  const theirs = conflictOf<Schemas["AppSetup"]>(save.error);
  const LABELS: Record<keyof typeof values, string> = { required_checks: "Required checks", target_platform: "Target platform",
    windows_check_pattern: "Windows validation check", lab_url: "Lab agent URL", launch: "Launch instructions" };
  return (
    <form aria-label="Release verification" onSubmit={(e: FormEvent) => { e.preventDefault(); save.mutate(draft.revision); }}>
      <DirtyNote dirty={draft.dirty} />
      <label>Required checks (one check name or glob per line; empty: every check reported on the commit)
        <textarea rows={3} placeholder={"build\ntest (*)"} value={values.required_checks} onChange={set("required_checks")} /></label>
      <label>Target platform
        <select value={values.target_platform} onChange={set("target_platform")}>
          {setup.platforms.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
        </select></label>
      <label>Windows validation check (for Windows desktop) <input value={values.windows_check_pattern} placeholder={setup.default_windows_pattern} onChange={set("windows_check_pattern")} /></label>
      <label>Lab agent URL (for the reset check) <input type="url" value={values.lab_url} onChange={set("lab_url")} /></label>
      <label>Launch instructions <textarea rows={3} value={values.launch} onChange={set("launch")} /></label>
      {theirs ? (
        <ConflictView busy={save.isPending}
          rows={(Object.keys(LABELS) as (keyof typeof values)[]).map((k) => ({ label: LABELS[k], mine: values[k], theirs: settingsOf(theirs.app)[k] }))}
          onKeepMine={() => save.mutate(theirs.app.revision)}
          onTakeTheirs={() => { draft.reset(settingsOf(theirs.app), theirs.app.revision); save.reset(); }} />
      ) : <ErrorText error={save.error} />}
      <button disabled={save.isPending}>Save verification settings</button>
    </form>
  );
}

function VerifyCommit({ appId, repo: initialRepo }: { appId: number; repo: string }) {
  const navigate = useNavigate();
  const [repo, setRepo] = useState(initialRepo);
  const [sha, setSha] = useState("");
  const [pr, setPr] = useState("");
  const verify = useAction(async () => data(await api.POST("/api/v1/apps/{app_id}/verify-commit", {
    params: { path: { app_id: appId } },
    body: { repo: repo.trim(), sha: sha.trim().toLowerCase(), pr_number: pr ? Number(pr) : undefined },
  })), (verification) => navigate(verification.release_id ? `/releases/${verification.release_id}#verification` : `/verifications/${verification.id}`));
  return (
    <form aria-label="Verify a commit" onSubmit={(e) => { e.preventDefault(); verify.mutate(undefined); }}>
      <label>Repository <input required placeholder="owner/name" value={repo} onChange={(e) => setRepo(e.target.value)} /></label>
      <label>Commit SHA <input required pattern="[0-9a-fA-F]{40}" value={sha} onChange={(e) => setSha(e.target.value)} /></label>
      <label>Pull request (optional) <input type="number" min={1} value={pr} onChange={(e) => setPr(e.target.value)} /></label>
      <ErrorText error={verify.error} />
      <button disabled={verify.isPending}>Verify this build</button>
    </form>
  );
}
