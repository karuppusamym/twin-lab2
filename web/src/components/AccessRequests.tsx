// Access requests: "Request access" on a 403 about an application (ErrorText shows it), and the
// home page's card with the requests the user decides and the outcome of their own.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, data, errorText, type Problem, type Role, type Schemas } from "../api/client";
import { when } from "../lib/format";

export const accessKey = ["access-requests"] as const;
const ROLES: Role[] = ["viewer", "builder", "owner"];
const STATE_WORDS: Record<Schemas["AccessRequestState"], string> = { pending: "waiting for an owner", approved: "approved", declined: "declined" };
const TONE: Record<Schemas["AccessRequestState"], string> = { pending: "review", approved: "done", declined: "neutral" };

function useAccessRequests() {
  return useQuery({ queryKey: accessKey, queryFn: async () => data(await api.GET("/api/v1/access-requests")) });
}

function appLabel(r: Schemas["AccessRequest"]) {
  return r.app_name ?? `Application ${r.app_id}`;
}

/** Under a 403 `role` about an application: who can grant the role, and a request for it. */
export function RequestAccess({ problem }: { problem: Problem }) {
  const appId = problem.app_id as number;
  const client = useQueryClient();
  const requests = useAccessRequests();
  const pending = requests.data?.mine.find((r) => r.app_id === appId && r.state === "pending");
  const [role, setRole] = useState<Role>(problem.requested_role ?? "viewer");
  const [reason, setReason] = useState("");
  const [open, setOpen] = useState(false);
  const send = useMutation({
    mutationFn: async () => data(await api.POST("/api/v1/apps/{app_id}/access-requests", { params: { path: { app_id: appId } }, body: { role, reason } })),
    onSuccess: async () => {
      setOpen(false);
      setReason("");
      await client.invalidateQueries({ queryKey: accessKey });
    },
  });
  const owners = problem.owners ?? [];
  return (
    <section className="nextcard" aria-label="Request access">
      <p>{owners.length ? <>The owners of this application: <b>{owners.join(", ")}</b>. They can give you a role.</> : <>The application's owners (or a portal admin) can give you a role.</>}</p>
      {send.isSuccess && <p className="notice" role="status">{send.data.message}</p>}
      {pending && !open ? (
        <p role="status">You asked for the <b>{pending.role}</b> role ({when(pending.created_at)}): {STATE_WORDS.pending}.{" "}
          <button type="button" className="secondary" onClick={() => { setRole(pending.role); setReason(pending.reason); setOpen(true); }}>Change the request</button></p>
      ) : !open ? (
        <p><button type="button" onClick={() => setOpen(true)}>Request access</button></p>
      ) : (
        <form aria-label="Request access" onSubmit={(e) => { e.preventDefault(); send.mutate(); }}>
          <label>Role
            <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
              {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
          <label>Why you need it <textarea required maxLength={500} rows={3} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="What you will do with the application" /></label>
          <div className="actions">
            <button type="submit" disabled={send.isPending || !reason.trim()}>Send the request</button>
            <button type="button" className="secondary" onClick={() => setOpen(false)}>Cancel</button>
          </div>
          {send.isError && <p className="error" role="alert">{errorText(send.error)}</p>}
        </form>
      )}
      <p className="muted">You will see the answer on your <Link to="/">home page</Link>.</p>
    </section>
  );
}

function Decide({ request }: { request: Schemas["AccessRequest"] }) {
  const client = useQueryClient();
  const [note, setNote] = useState("");
  const decide = useMutation({
    mutationFn: async (decision: "approve" | "decline") => data(await api.POST("/api/v1/access-requests/{request_id}/decision", {
      params: { path: { request_id: request.id } }, body: { decision, note },
    })),
    onSuccess: () => Promise.all([accessKey, ["session"], ["app", request.app_id], ["home"]].map((queryKey) => client.invalidateQueries({ queryKey }))),
  });
  const who = `${request.user} (${request.role} on ${appLabel(request)})`;
  return (
    <li>
      <p><b>{request.user}</b> asks for the <b>{request.role}</b> role on <Link to={`/apps/${request.app_id}#access`}>{appLabel(request)}</Link> <span className="muted">{when(request.created_at)}</span></p>
      <blockquote>{request.reason}</blockquote>
      <div className="actions">
        <label className="grow">Note to {request.user} (optional) <input maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} /></label>
        <button type="button" disabled={decide.isPending} onClick={() => decide.mutate("approve")} aria-label={`Approve ${who}`}>Approve</button>
        <button type="button" className="secondary" disabled={decide.isPending} onClick={() => decide.mutate("decline")} aria-label={`Decline ${who}`}>Decline</button>
      </div>
      {decide.isError && <p className="error" role="alert">{errorText(decide.error)}</p>}
    </li>
  );
}

/** The home page's card: requests to decide (owners, admins) and the outcome of the user's own. */
export function AccessRequestsCard() {
  const requests = useAccessRequests();
  const list = requests.data;
  if (!list || (!list.to_decide.length && !list.mine.length)) return null;
  const recent = list.mine.slice(0, 5);
  return (
    <section aria-labelledby="access-h" id="access-requests">
      <h2 id="access-h">Access requests</h2>
      {list.to_decide.length > 0 && (
        <ul className="list" aria-label="Access requests to decide">{list.to_decide.map((r) => <Decide key={r.id} request={r} />)}</ul>
      )}
      {recent.length > 0 && (
        <ul className="list" aria-label="Your access requests">{recent.map((r) => (
          <li key={r.id}>
            <span className={`badge tone-${TONE[r.state]}`}>{STATE_WORDS[r.state]}</span>{" "}
            {r.role} on {r.app_name ? <Link to={`/apps/${r.app_id}`}>{r.app_name}</Link> : appLabel(r)}
            {r.decided_by && <> by {r.decided_by} <span className="muted">{when(r.decided_at)}</span></>}
            {r.note && <>: “{r.note}”</>}
          </li>
        ))}</ul>
      )}
    </section>
  );
}
