import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, data } from "../api/client";
import { QueryView, useTitle } from "../components/ui";
import { useCan } from "../session";

export function SetupPage() {
  useTitle("Setup");
  const can = useCan();
  const setup = useQuery({ queryKey: ["setup"], queryFn: async () => data(await api.GET("/api/v1/setup")) });
  return (
    <>
      <h1>Setup</h1>
      <p>For the people who run the portal and the recorders. An application's own repository and verification settings are steps of its journey (its page links to them); they live on <em>its</em> setup page, listed below.</p>
      <QueryView query={setup}>
        {(s) => (
          <div className="columns">
            <section aria-label="Portal">
              <h2>Recorder machines</h2>
              <p><Link to="/machines">Machines and pairing</Link>: make a pairing code for a PC's recorder, see when each machine was last seen, revoke one.</p>
              <h2>Build sessions</h2>
              <p>{can("admin") ? <Link to="/setup/builds">Build sessions</Link> : "Build sessions"}: provider (Devin, Claude, Copilot) and GitHub credentials, repository connections, storage and the build worker{can("admin") ? "" : " (portal admins)"}.</p>
              <h2>Automations</h2>
              <p><Link to="/automations">Automations</Link>: the bots and processes recordings are made for, and the applications they touch.</p>
              {s.storage && (
                <>
                  <h2>Artifact storage</h2>
                  <p className="muted">Where evidence, frozen build packs, reports and release downloads are kept. Set with <code>TWINLAB2_ARTIFACT_BACKEND</code> and the <code>TWINLAB2_S3_*</code> variables; credentials come from the environment and are never shown here.</p>
                  <dl id="artifact-storage">
                    {Object.entries(s.storage).map(([name, value]) => (
                      <div key={name}><dt>{name.replace(/_/g, " ")}</dt><dd>{value === true ? "yes" : value === false ? "no" : String(value ?? "")}</dd></div>
                    ))}
                  </dl>
                </>
              )}
            </section>
            <section aria-labelledby="apps-h">
              <h2 id="apps-h">Applications</h2>
              <p className="muted">Identities, Git repository and deployment configuration of each application.</p>
              {s.apps.length ? s.apps.map((a) => <p key={a.id}><Link to={`/apps/${a.id}/setup`}>{a.name}</Link></p>) : <p>No applications yet.</p>}
            </section>
          </div>
        )}
      </QueryView>
    </>
  );
}
