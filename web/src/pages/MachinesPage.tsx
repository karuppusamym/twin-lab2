import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, data, type Schemas } from "../api/client";
import { ActionButton, Badge, Crumbs, ErrorText, QueryView, useAction, useTitle } from "../components/ui";
import { when } from "../lib/format";

const SCOPE_WORDS: Record<Schemas["AgentScope"], string> = { record: "record (sessions and references)", upload: "upload evidence" };

export function MachinesPage() {
  useTitle("Machines");
  const list = useQuery({ queryKey: ["machines"], queryFn: async () => data(await api.GET("/api/v1/machines")) });
  // the code is shown once: it lives only in this mutation's result
  const [scopes, setScopes] = useState<Schemas["AgentScope"][]>(["record", "upload"]);
  const pair = useAction(async () => data(await api.POST("/api/v1/machines/pairing-codes", { body: { scopes } })));
  const toggle = (scope: Schemas["AgentScope"], on: boolean) => setScopes((s) => (on ? [...s, scope] : s.filter((x) => x !== scope)));
  return (
    <>
      <Crumbs><Link to="/setup">Setup / Advanced</Link></Crumbs>
      <h1>Machines</h1>
      <section aria-labelledby="pair-h">
        <h2 id="pair-h">Pair a machine</h2>
        {pair.data && (
          <>
            <p>Enter this code in TwinLabRecorder on the machine within {pair.data.minutes} minutes. It works once and is not shown again.</p>
            <p className="code" id="pair-code">{pair.data.display}</p>
          </>
        )}
        <ErrorText error={pair.error} />
        <fieldset>
          <legend>The machine may</legend>
          <label className="inline-check"><input type="checkbox" checked={scopes.includes("record")} onChange={(e) => toggle("record", e.target.checked)} /> {SCOPE_WORDS.record}</label>
          <label className="inline-check"><input type="checkbox" checked={scopes.includes("upload")} onChange={(e) => toggle("upload", e.target.checked)} /> {SCOPE_WORDS.upload}</label>
        </fieldset>
        <button type="button" disabled={pair.isPending || !scopes.length} onClick={() => pair.mutate(undefined)}>Make a pairing code</button>
      </section>
      <section aria-labelledby="paired-h">
        <h2 id="paired-h">Paired machines</h2>
        <QueryView query={list}>
          {(l) => l.machines.length ? (
            <>{l.machines.map((m) => (
              <div className="row" key={m.id}>
                <strong>{m.name}</strong>
                <span className="muted">paired {when(m.paired_at)} by {m.paired_by}{m.last_seen && ` · seen ${when(m.last_seen)}`}{m.recorder && ` · ${m.recorder}`}{m.contract_version && ` · contract ${m.contract_version}`} · may {m.scopes.map((x) => SCOPE_WORDS[x]).join(" and ")}</span>
                {m.revoked_at ? <Badge kind="not_replicated">revoked</Badge> : (
                  <ActionButton secondary label="Revoke" name={`Revoke ${m.name}`} confirm={`Revoke ${m.name}? Its recorder can no longer send recordings.`}
                    run={async () => data(await api.POST("/api/v1/machines/{machine_id}/revoke", { params: { path: { machine_id: m.id } } }))} />
                )}
              </div>
            ))}</>
          ) : <p>No machine is paired yet.</p>}
        </QueryView>
      </section>
    </>
  );
}
