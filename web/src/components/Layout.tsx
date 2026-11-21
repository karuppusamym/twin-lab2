import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, type ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { api } from "../api/client";
import type { Session } from "../api/client";
import { stackTables } from "../lib/stackTables";
import { sessionKey, useSession } from "../session";

/** Scrolls to the #section of the address once it is rendered (journey links point at sections). */
function ScrollToHash() {
  const { hash, pathname } = useLocation();
  const first = useRef(true);
  useEffect(() => {
    // after a link to another page, start the keyboard and screen reader at its content (not at
    // the link that was followed, which is gone); the first page load starts at the top as usual
    const moved = !first.current;
    first.current = false;
    if (!hash) {
      window.scrollTo?.(0, 0);
      if (moved) document.getElementById("main")?.focus({ preventScroll: true });
      return;
    }
    let tries = 0;
    let timer: ReturnType<typeof setTimeout>;
    const look = () => {
      const target = document.getElementById(decodeURIComponent(hash.slice(1)));
      if (target) {
        target.scrollIntoView?.();
        if (target instanceof HTMLDetailsElement) target.open = true;
      } else if (tries++ < 20) {
        timer = setTimeout(look, 100);
      }
    };
    look();
    return () => clearTimeout(timer);
  }, [hash, pathname]);
  return null;
}

function SignOut() {
  const client = useQueryClient();
  return (
    <button
      type="button"
      className="secondary"
      onClick={async () => {
        await api.DELETE("/api/v1/session");
        // forget every page of the signed-in user, then ask who is signed in now
        client.removeQueries({ predicate: (q) => q.queryKey[0] !== sessionKey[0] });
        await client.refetchQueries({ queryKey: sessionKey });
      }}
    >
      Sign out
    </button>
  );
}

/** The highest role the user holds anywhere (the server's `home_role`, else from `roles`). */
export function homeRole(session: Session): Session["home_role"] & string {
  if (session.home_role) return session.home_role;
  if (session.is_admin) return "admin";
  const rank = { viewer: 1, builder: 2, owner: 3 } as const;
  const top = session.roles.reduce((best, r) => (rank[r.role] > (best ? rank[best] : 0) ? r.role : best), null as null | keyof typeof rank);
  return top ?? "none";
}

export function Layout({ children }: { children: ReactNode }) {
  const session = useSession();
  const main = useRef<HTMLElement>(null);
  useEffect(() => (main.current ? stackTables(main.current) : undefined), []);
  const role = homeRole(session);
  const owner = role === "owner" || role === "admin";
  // what each role can use: viewers read applications and releases; owners review recordings,
  // request new ones and decide scope; admins also set the portal up
  return (
    <>
      <a className="skip" href="#main">Skip to content</a>
      <header>
        <Link className="brand" to="/">TwinLab2</Link>
        <nav aria-label="Main">
          <NavLink to="/" end>Home</NavLink>
          <NavLink to="/apps">Applications</NavLink>
          {owner && <NavLink to="/recordings">Recordings</NavLink>}
          {owner && <NavLink to="/triage">Scope triage</NavLink>}
          {owner && <NavLink to="/sessions">Request a recording</NavLink>}
          {owner && <NavLink to="/setup">Setup</NavLink>}
          {session.is_admin && <NavLink to="/admin/users">Users</NavLink>}
          {session.is_admin && <NavLink to="/admin/operations">Operations</NavLink>}
        </nav>
        <div className="account">
          <span className="who">{session.user}{session.is_admin ? " (admin)" : ""}</span>
          {session.auth_mode === "local" && session.login_available && <SignOut />}
        </div>
      </header>
      <main id="main" tabIndex={-1} ref={main}>
        <ScrollToHash />
        {children}
      </main>
    </>
  );
}
