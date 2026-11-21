import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useDeferredValue, useState } from "react";
import { Link } from "react-router-dom";
import { api, data, type Schemas } from "../api/client";
import { Badge, Pager, QueryView, useTitle } from "../components/ui";
import { when } from "../lib/format";

export function statusKind(status: Schemas["RecordingStatus"]) {
  if (status.label === "Complete") return "recorded";
  return status.attention ? "discovered" : "packaged";
}

export function RecordingsPage() {
  useTitle("Recordings");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const search = useDeferredValue(q.trim());
  const list = useQuery({
    queryKey: ["recordings", search, offset],
    queryFn: async () => data(await api.GET("/api/v1/recordings", { params: { query: { q: search || undefined, offset, limit: 50 } } })),
    placeholderData: keepPreviousData,
  });
  return (
    <>
      <h1>Recordings</h1>
      <p>A recording stays on the machine that made it; the portal holds its references: the applications recorded and each one's steps.</p>
      <section aria-label="Recordings">
        <form role="search" aria-label="Find recordings" className="toolbar" onSubmit={(e) => e.preventDefault()}>
          <label>Search <input type="search" value={q} placeholder="Recording name" onChange={(e) => { setQ(e.target.value); setOffset(0); }} /></label>
        </form>
        <QueryView query={list}>
          {(l) => l.recordings.length ? (
            <>{l.recordings.map((r) => (
              <div className="row" key={r.id}>
                <Link to={`/recordings/${encodeURIComponent(r.id)}`}><strong>{r.name}</strong></Link>
                <span className="muted">{r.machine ? `${r.machine} · ${when(r.received_at)}` : "imported"}{r.automation && ` · ${r.automation}`}</span>
                {r.apps && <span>{r.apps}</span>}
                <span title={r.status.summary}><Badge kind={statusKind(r.status)}>{r.status.label}</Badge></span>
              </div>
            ))}<Pager page={l.page} onOffset={setOffset} label="Recording pages" /></>
          ) : <p>{search ? "No recording matches." : "No recordings yet."}</p>}
        </QueryView>
      </section>
    </>
  );
}
