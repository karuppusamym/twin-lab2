import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api, data, setCsrfToken } from "../api/client";
import { ErrorText, useAction, useTitle } from "../components/ui";
import { sessionKey } from "../session";

/** Local sign-in (shown wherever the address points while nobody is signed in). */
export function LoginPage() {
  useTitle("Sign in");
  const client = useQueryClient();
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const signIn = useAction(
    async () => data(await api.POST("/api/v1/session", { body: { user: user.trim(), password } })),
    (session) => {
      setCsrfToken(session.csrf_token);
      client.setQueryData(sessionKey, session);
    },
  );
  const submit = (event: FormEvent) => {
    event.preventDefault();
    signIn.mutate(undefined);
  };
  return (
    <>
      <header><span className="brand">TwinLab2</span></header>
      <main id="main">
        <h1>Sign in</h1>
        <form onSubmit={submit} aria-label="Sign in">
          <ErrorText error={signIn.error} />
          <label>Your name <input name="user" required maxLength={80} autoComplete="username" value={user} onChange={(e) => setUser(e.target.value)} /></label>
          <label>Password <input name="password" type="password" required autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
          <button disabled={signIn.isPending}>Sign in</button>
        </form>
      </main>
    </>
  );
}
