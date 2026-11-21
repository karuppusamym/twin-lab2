import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, data, type Schemas } from "../api/client";
import { ActionButton, Crumbs, ErrorText, Notice, QueryView, useAction, useTitle } from "../components/ui";

type Secret = "DEVIN_API_KEY" | "GITHUB_TOKEN" | "ANTHROPIC_API_KEY" | "COPILOT_GITHUB_TOKEN";

/** Build sessions setup (portal admins). Keys and tokens are never shown back. */
export function BuildSetupPage() {
  useTitle("Build sessions setup");
  const setup = useQuery({ queryKey: ["setup", "builds"], queryFn: async () => data(await api.GET("/api/v1/setup/builds")) });
  return (
    <>
      <Crumbs><Link to="/setup">Setup / Advanced</Link> · Build sessions</Crumbs>
      <h1>Build sessions: Setup</h1>
      <p>Credentials, repository connections, storage and the build worker. Keys and tokens are never shown back:
        an environment variable wins over a value saved here, and saved values are kept in the catalog database, so
        prefer environment variables (or a secret mount) in production.</p>
      <QueryView query={setup}>{(s) => <SetupView setup={s} />}</QueryView>
    </>
  );
}

function SettingsForm({ label, fields, secrets, setup }: {
  label: string;
  fields: { key: string; label: string; type?: string; placeholder?: string; min?: string; max?: string; step?: string }[];
  secrets: Secret[];
  setup: Schemas["BuildSetup"];
}) {
  const initial = Object.fromEntries(fields.map((f) => [f.key, (secrets as string[]).includes(f.key) ? "" : setup.settings[f.key] ?? ""]));
  const [values, setValues] = useState<Record<string, string>>(initial);
  const [clear, setClear] = useState<Secret[]>([]);
  const save = useAction(async () => {
    // an empty secret box keeps the saved secret
    const settings = Object.fromEntries(Object.entries(values).filter(([k, v]) => !(secrets as string[]).includes(k) || v));
    return data(await api.PUT("/api/v1/setup/builds/settings", { body: { settings, clear } }));
  }, () => setClear([]));
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate(undefined); };
  return (
    <form aria-label={label} autoComplete="off" onSubmit={submit}>
      {fields.map((f) => {
        const secret = (secrets as string[]).includes(f.key);
        return (
          <div key={f.key}>
            <label>{f.label}{" "}
              <input type={secret ? "password" : f.type ?? "text"} min={f.min} max={f.max} step={f.step} value={values[f.key] ?? ""}
                placeholder={secret && setup.saved[f.key] ? "saved: leave empty to keep it" : f.placeholder}
                onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))} />
            </label>
            {secret && (
              <label className="check">
                <input type="checkbox" checked={clear.includes(f.key as Secret)} onChange={(e) => setClear((c) => (e.target.checked ? [...c, f.key as Secret] : c.filter((x) => x !== f.key)))} />
                {" "}Remove the saved {f.label.replace(/ \(.*\)$/, "")}
              </label>
            )}
          </div>
        );
      })}
      {save.isSuccess && <Notice>Saved.</Notice>}
      <ErrorText error={save.error} />
      <button disabled={save.isPending}>Save</button>
    </form>
  );
}

function SetupView({ setup }: { setup: Schemas["BuildSetup"] }) {
  const check = useAction(async () => data(await api.POST("/api/v1/setup/builds/check")));
  const worker = useAction(async () => data(await api.POST("/api/v1/setup/builds/worker")));
  const [repo, setRepo] = useState("");
  const connect = useAction(async () => data(await api.POST("/api/v1/setup/builds/repos", { body: { repo: repo.trim() } })), () => setRepo(""));
  const stale = setup.workers.filter((w) => w.state === "stale").length;
  return (
    <>
      <div className="columns">
        <section id="credentials" aria-labelledby="cred-h">
          <h2 id="cred-h">Credentials</h2>
          {setup.devin_error && <p className="error">{setup.devin_error}</p>}
          <dl className="about">
            <dt>Devin API key</dt><dd id="devin-key">{setup.devin.api_key_set ? `set (${setup.devin.api_key_source})` : "not set"}</dd>
            <dt>Devin organisation</dt><dd>{setup.devin.org_id ?? "not set"}{setup.devin.org_id_source && ` (${setup.devin.org_id_source})`}</dd>
            <dt>Devin API</dt><dd>{setup.devin.base_url}</dd>
            <dt>GitHub token</dt><dd id="github-token">{setup.github.token_set ? `set (${setup.github.source})` : "not set"}</dd>
            <dt>GitHub API</dt><dd>{setup.github.api}</dd>
          </dl>
          <SettingsForm label="Devin and GitHub" setup={setup} secrets={["DEVIN_API_KEY", "GITHUB_TOKEN"]} fields={[
            { key: "DEVIN_API_KEY", label: "Devin API key", placeholder: "cog_…" },
            { key: "DEVIN_ORG_ID", label: "Devin organisation id" },
            { key: "DEVIN_BASE_URL", label: "Devin API address", placeholder: "https://api.devin.ai" },
            { key: "GITHUB_TOKEN", label: "GitHub token", placeholder: "a fine-grained token with read access" },
            { key: "GITHUB_API", label: "GitHub API address", placeholder: "https://api.github.com" },
            { key: "DEFAULT_ACU_BUDGET", label: "Default ACU budget per build", type: "number", min: "1", max: "1000" },
          ]} />
          <button type="button" className="secondary" disabled={check.isPending} onClick={() => check.mutate(undefined)}>Check Devin and GitHub</button>
          {check.data?.results.map((r) => <p key={r.service} className={r.ok ? "notice" : "error"}>{r.message}</p>)}
          <ErrorText error={check.error} />
        </section>

        <section id="providers" aria-labelledby="prov-h">
          <h2 id="prov-h">Providers</h2>
          <table className="flow">
            <thead><tr><th scope="col">Provider</th><th scope="col">Status</th><th scope="col">Capabilities</th><th scope="col">Here</th></tr></thead>
            <tbody>{setup.providers.map((p) => (
              <tr key={p.name}><td>{p.label}</td><td>{p.status_label}</td><td>{p.capability_text}</td><td>{p.ready ? <span className="error">{p.ready}</span> : "ready"}</td></tr>
            ))}</tbody>
          </table>
          <SettingsForm label="Providers" setup={setup} secrets={["ANTHROPIC_API_KEY", "COPILOT_GITHUB_TOKEN"]} fields={[
            { key: "ANTHROPIC_API_KEY", label: "Anthropic API key (Claude sessions)", placeholder: "sk-ant-…" },
            { key: "CLAUDE_MODEL", label: "Claude model", placeholder: "claude-opus-5-5" },
            { key: "DEFAULT_CLAUDE_BUDGET_USD", label: "Default Claude budget per session (USD)", type: "number", min: "0.5", max: "500", step: "any" },
            { key: "COPILOT_GITHUB_TOKEN", label: "Copilot GitHub token (not validated)", placeholder: "a Copilot-entitled token" },
            { key: "COPILOT_MODEL", label: "Copilot model", placeholder: "the Copilot default" },
          ]} />
          <p className="muted">Claude and Copilot sessions run in an isolated process per session on the worker machine, which needs
            <code> pip install -r requirements-providers.txt</code> and git. Copilot is off unless <code>TWINLAB2_ENABLE_COPILOT=1</code>.</p>
        </section>

        <section id="repos" aria-labelledby="repos-h">
          <h2 id="repos-h">GitHub repositories</h2>
          <p className="muted">Builds can target only a connected repository. Connecting checks that the token can see it.
            Devin also needs its own access to the repository (its GitHub integration).</p>
          {setup.repos.length ? setup.repos.map((r) => (
            <div className="row" key={r.repo}>
              <span><a href={`https://github.com/${r.repo}`}>{r.repo}</a> · default <code>{r.default_branch}</code>{r.private && " · private"} · by {r.connected_by}</span>
              <ActionButton secondary label="Disconnect" confirm={`Disconnect ${r.repo}?`} run={async () => {
                await api.DELETE("/api/v1/setup/builds/repos", { params: { query: { repo: r.repo } } });
              }} />
            </div>
          )) : <p>No repository connected.</p>}
          <form aria-label="Connect a repository" onSubmit={(e) => { e.preventDefault(); connect.mutate(undefined); }}>
            <label>Repository <input required placeholder="owner/name" value={repo} onChange={(e) => setRepo(e.target.value)} /></label>
            <ErrorText error={connect.error} />
            <button disabled={connect.isPending}>Connect</button>
          </form>
          <h2>Storage</h2>
          <p className="muted">Build packs are stored in the catalog database by revision, and a build uploads its frozen pack to Devin as a session attachment.</p>
        </section>
      </div>

      <section id="worker" aria-labelledby="worker-h">
        <h2 id="worker-h">Build worker</h2>
        <p>Builds run in one worker process beside the portal: <code>python worker.py --database &lt;catalog&gt;</code>. A job held
          by a worker that stopped is taken over once its {setup.lease_seconds}-second lease runs out.</p>
        {setup.dev_worker && (
          <>
            <button type="button" className="secondary" disabled={worker.isPending} onClick={() => worker.mutate(undefined)}>Start a worker (development)</button>
            {worker.data && <Notice>Worker {worker.data.started ? "started" : "already running"} (process {worker.data.pid}); its log is worker.log beside the catalog.</Notice>}
            <ErrorText error={worker.error} />
          </>
        )}
        <h3>Workers seen</h3>
        {stale > 0 && <p className="muted">{stale} not seen recently: it may have died.</p>}
        <table className="flow">
          <thead><tr><th scope="col">Worker</th><th scope="col">Started</th><th scope="col">Last seen</th><th scope="col">Job</th><th scope="col">State</th></tr></thead>
          <tbody>{setup.workers.length ? setup.workers.map((w) => (
            <tr key={w.id}><td><code>{w.id}</code></td><td>{w.started_at.slice(0, 19)}</td><td>{w.seen_at.slice(0, 19)}</td><td>{(w.current_job ?? "").slice(0, 8)}</td>
              <td>{w.state === "stale" ? "not seen recently: it may have died" : w.state}</td></tr>
          )) : <tr><td colSpan={5}>No worker has run on this catalog.</td></tr>}</tbody>
        </table>
        <h3>Jobs</h3>
        <p>{["queued", "running", "done", "failed"].map((s) => `${s}: ${setup.job_counts[s] ?? 0}`).join(" · ")}</p>
        <table className="flow">
          <thead><tr><th scope="col">Job</th><th scope="col">Type</th><th scope="col">State</th><th scope="col">Attempts</th><th scope="col">Due / lease</th><th scope="col">Error</th></tr></thead>
          <tbody>{setup.jobs.length ? setup.jobs.map((j) => (
            <tr key={j.id}><td><code>{j.id.slice(0, 8)}</code></td><td>{j.type}</td><td>{j.state}</td><td>{j.attempts}</td>
              <td>{(j.due ?? "").slice(0, 19)}{j.lease_owner && ` · ${j.lease_owner}`}</td><td className="muted">{j.error ?? ""}</td></tr>
          )) : <tr><td colSpan={6}>No jobs yet.</td></tr>}</tbody>
        </table>
      </section>
    </>
  );
}
