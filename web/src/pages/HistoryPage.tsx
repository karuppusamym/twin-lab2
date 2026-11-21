import { useQuery } from "@tanstack/react-query";
import { Fragment } from "react";
import { Link } from "react-router-dom";
import { api, data } from "../api/client";
import { Crumbs, QueryView, useTitle } from "../components/ui";
import { useAppId } from "./AppPage";

function shown(value: unknown) {
  return !(value === null || value === undefined || value === "" || (Array.isArray(value) && value.length === 0)
    || (typeof value === "object" && !Array.isArray(value) && Object.keys(value as object).length === 0));
}

export function HistoryPage() {
  const appId = useAppId();
  const history = useQuery({
    queryKey: ["app", appId, "history"],
    queryFn: async () => data(await api.GET("/api/v1/apps/{app_id}/history", { params: { path: { app_id: appId } } })),
  });
  useTitle(history.data ? `History · ${history.data.app.name}` : undefined);
  return (
    <QueryView query={history}>
      {(h) => (
        <>
          <Crumbs><Link to={`/apps/${appId}`}>{h.app.name}</Link></Crumbs>
          <h1>Audit history: {h.app.name}</h1>
          <p className="muted">Append-only: entries are never changed or removed.</p>
          <div className="scroll">
            <table className="flow" id="history">
              <thead><tr><th scope="col">When</th><th scope="col">Who</th><th scope="col">Action</th><th scope="col">Object</th><th scope="col">Details</th></tr></thead>
              <tbody>
                {h.entries.length ? h.entries.map((row) => {
                  const details = Object.entries(row.details).filter(([, v]) => shown(v));
                  return (
                    <tr key={row.id}>
                      <td>{row.at.replace("T", " ")}</td><td>{row.actor}</td><td><code>{row.action}</code></td>
                      <td>{row.object_type} {row.object_id}</td>
                      <td>{details.map(([k, v], i) => (
                        <Fragment key={k}><span className="muted">{k}</span> {typeof v === "string" ? v : JSON.stringify(v)}{i < details.length - 1 ? " · " : ""}</Fragment>
                      ))}</td>
                    </tr>
                  );
                }) : <tr><td colSpan={5}>Nothing recorded yet.</td></tr>}
              </tbody>
            </table>
          </div>
        </>
      )}
    </QueryView>
  );
}
