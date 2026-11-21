import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api, data } from "../api/client";
import { ErrorText, QueryView, useAction, useTitle } from "../components/ui";

export function UsersPage() {
  useTitle("Users");
  const users = useQuery({ queryKey: ["users"], queryFn: async () => data(await api.GET("/api/v1/admin/users")) });
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [admin, setAdmin] = useState(false);
  const save = useAction(async () => data(await api.PUT("/api/v1/admin/users/{user_name}", {
    params: { path: { user_name: name.trim() } }, body: { password: password || undefined, is_admin: admin },
  })), () => { setName(""); setPassword(""); setAdmin(false); });
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate(undefined); };
  return (
    <>
      <h1>Users and portal admins</h1>
      <p className="muted">The local user store for development sign-in. Behind an SSO proxy, people sign in there and need no password here; mark admins here or with <code>TWINLAB2_ADMINS</code>. Roles per application are assigned on each application's page.</p>
      <QueryView query={users}>
        {(u) => (
          <table className="flow">
            <thead><tr><th scope="col">User</th><th scope="col">Password</th><th scope="col">Portal admin</th><th scope="col">Added</th></tr></thead>
            <tbody>{u.users.length ? u.users.map((x) => (
              <tr key={x.name}><td>{x.name}</td><td>{x.has_password ? "set" : "none"}</td><td>{x.is_admin ? "yes" : "no"}</td><td>{x.created_by} {x.created_at.replace("T", " ")}</td></tr>
            )) : <tr><td colSpan={4}>No local users.</td></tr>}</tbody>
          </table>
        )}
      </QueryView>
      <form aria-label="Save user" onSubmit={submit}>
        <label>Sign-in name <input required maxLength={120} value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label>Password (leave empty to keep) <input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
        <label className="check"><input type="checkbox" checked={admin} onChange={(e) => setAdmin(e.target.checked)} /> Portal admin</label>
        <ErrorText error={save.error} />
        <button disabled={save.isPending}>Save user</button>
      </form>
    </>
  );
}
