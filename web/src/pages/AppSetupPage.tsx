import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, data, type Schemas } from "../api/client";
import { Crumbs, ErrorText, QueryView, useAction, useField, useTitle } from "../components/ui";
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
          <GitForm appId={app.id} gitUrl={app.git_url} />
        </section>
      </div>
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
  const [match, setMatch] = useField(setup.app.url_match);
  const add = useAction(async () => data(await api.POST("/api/v1/apps/{app_id}/identities", { params: { path: { app_id: appId } }, body: { key: key.trim() } })), () => setKey(""));
  const saveMatch = useAction(async () => data(await api.PUT("/api/v1/apps/{app_id}/url-match", { params: { path: { app_id: appId } }, body: { url_match: match as "path" | "host" } })));
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
        <form aria-label="Web address matching" onSubmit={(e) => { e.preventDefault(); saveMatch.mutate(undefined); }}>
          <label>Web addresses match
            <select value={match} onChange={(e) => setMatch(e.target.value as "path" | "host")}>
              <option value="path">by path (the longest matching path wins)</option>
              <option value="host">the whole host</option>
            </select>
          </label>
          <ErrorText error={saveMatch.error} />
          <button className="secondary" disabled={saveMatch.isPending}>Save matching</button>
        </form>
      )}
    </section>
  );
}

function GitForm({ appId, gitUrl }: { appId: number; gitUrl: string }) {
  const [url, setUrl] = useField(gitUrl);
  const save = useAction(async () => data(await api.PATCH("/api/v1/apps/{app_id}", { params: { path: { app_id: appId } }, body: { git_url: url.trim() } })));
  return (
    <form aria-label="Git repository" onSubmit={(e) => { e.preventDefault(); save.mutate(undefined); }}>
      <label>Demo repository <input maxLength={500} value={url} onChange={(e) => setUrl(e.target.value)} /></label>
      <ErrorText error={save.error} />
      <button disabled={save.isPending}>Save repository</button>
    </form>
  );
}

function VerificationForm({ setup }: { setup: Schemas["AppSetup"] }) {
  const app = setup.app;
  const [values, setValues] = useState({
    required_checks: app.required_checks, target_platform: app.target_platform,
    windows_check_pattern: app.windows_check_pattern, lab_url: app.lab_url, launch: app.launch,
  });
  const set = (k: keyof typeof values) => (e: { target: { value: string } }) => setValues((v) => ({ ...v, [k]: e.target.value }));
  const save = useAction(async () => data(await api.PUT("/api/v1/apps/{app_id}/verification-settings", {
    params: { path: { app_id: app.id } },
    body: { ...values, target_platform: values.target_platform as Schemas["VerificationSettings"]["target_platform"] },
  })));
  return (
    <form aria-label="Release verification" onSubmit={(e: FormEvent) => { e.preventDefault(); save.mutate(undefined); }}>
      <label>Required checks (one check name or glob per line; empty: every check reported on the commit)
        <textarea rows={3} placeholder={"build\ntest (*)"} value={values.required_checks} onChange={set("required_checks")} /></label>
      <label>Target platform
        <select value={values.target_platform} onChange={set("target_platform")}>
          {setup.platforms.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
        </select></label>
      <label>Windows validation check (for Windows desktop) <input value={values.windows_check_pattern} placeholder={setup.default_windows_pattern} onChange={set("windows_check_pattern")} /></label>
      <label>Lab agent URL (for the reset check) <input type="url" value={values.lab_url} onChange={set("lab_url")} /></label>
      <label>Launch instructions <textarea rows={3} value={values.launch} onChange={set("launch")} /></label>
      <ErrorText error={save.error} />
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
  })), (outcome) => navigate(`/releases/${outcome.release_id}#verification`));
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
