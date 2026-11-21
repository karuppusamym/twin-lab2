import { useQueryClient } from "@tanstack/react-query";
import { useEffect, type ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { api } from "../api/client";
import { sessionKey, useSession } from "../session";

/** Scrolls to the #section of the address once it is rendered (journey links point at sections). */
function ScrollToHash() {
  const { hash, pathname } = useLocation();
  useEffect(() => {
    if (!hash) {
      window.scrollTo?.(0, 0);
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

export function Layout({ children }: { children: ReactNode }) {
  const session = useSession();
  return (
    <>
      <a className="skip" href="#main">Skip to content</a>
      <header>
        <Link className="brand" to="/">TwinLab2</Link>
        <nav aria-label="Main">
          <NavLink to="/" end>Applications</NavLink>
          <NavLink to="/recordings">Recordings</NavLink>
          <NavLink to="/sessions">Request a recording</NavLink>
          <NavLink to="/setup">Setup / Advanced</NavLink>
          {session.is_admin && <NavLink to="/admin/users">Users</NavLink>}
          <span className="who">{session.user}{session.is_admin ? " (admin)" : ""}</span>
          {session.auth_mode === "local" && session.login_available && <SignOut />}
        </nav>
      </header>
      <main id="main" tabIndex={-1}>
        <ScrollToHash />
        {children}
      </main>
    </>
  );
}
