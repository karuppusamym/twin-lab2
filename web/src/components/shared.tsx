// Pieces several pages show: the journey, recorded steps, package files, the tested release.
import { Link } from "react-router-dom";
import type { Schemas } from "../api/client";
import { Badge } from "./ui";

/** A journey href is a web-app path, possibly with a #section. */
export function JourneyLink({ href, className, title, children }: { href: string; className?: string; title?: string; children: React.ReactNode }) {
  return <Link to={href} className={className} title={title}>{children}</Link>;
}

export function JourneyNav({ journey, name }: { journey: Schemas["Journey"]; name: string }) {
  const last = journey.steps[journey.steps.length - 1];
  return (
    <>
      <nav className="progress" aria-label={`Progress of ${name}`}>
        {journey.steps.map((s, i) => (
          <JourneyLink key={s.id} href={s.href} className={s.state} title={s.hint}>
            <span className="tick" aria-hidden="true">{s.state === "done" ? "✓" : i + 1}</span>
            {s.title}
            {s.state === "done" && <span className="visually-hidden"> (done)</span>}
            {s.state === "next" && <span className="visually-hidden"> (next step)</span>}
          </JourneyLink>
        ))}
      </nav>
      {journey.out_of_scope ? (
        <div className="nextcard">
          <div className="nexthead">Not replicated</div>
          <p>Recordings note this application only as having been used. To replicate it, change its scope decision to <em>Confirmed application</em>. <a className="button" href="#choose">Change scope</a></p>
        </div>
      ) : journey.next ? (
        <div className="nextcard">
          <div className="nexthead">Next step</div>
          <p><b>{journey.next.title}:</b> {journey.next.hint} <JourneyLink className="button" href={journey.next.href}>{journey.next.action}</JourneyLink></p>
        </div>
      ) : (
        <div className="nextcard">
          <div className="nexthead">Ready</div>
          <p>The latest release is published. {last && <JourneyLink className="button" href={last.href}>Open or download</JourneyLink>}</p>
        </div>
      )}
    </>
  );
}

/** Recorded steps of one application (the old _flow.html). */
export function FlowTable({ steps, caption }: { steps: Schemas["FlowStep"][]; caption?: string }) {
  return (
    <div className="scroll">
      <table className="flow">
        {caption && <caption className="visually-hidden">{caption}</caption>}
        <thead><tr><th scope="col">#</th><th scope="col">Action</th><th scope="col">Control</th><th scope="col">Label</th><th scope="col">Screen</th></tr></thead>
        <tbody>
          {steps.map((st) => (
            <tr key={st.event_id ?? st.n}>
              <td>{st.seq ?? st.n}</td>
              <td>{st.action}{st.key ? ` ${st.key}` : ""}</td>
              <td>{st.control ?? ""}</td>
              <td>{st.label ?? ""}</td>
              <td>{st.screen ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The files of a package: path, size and SHA-256; with `full`, text files in full below. */
export function PackageFiles({ files, full }: { files: Schemas["PackageFile"][]; full?: boolean }) {
  return (
    <>
      <div className="scroll">
        <table className="flow">
          <thead><tr><th scope="col">File</th><th scope="col">Bytes</th><th scope="col">SHA-256</th></tr></thead>
          <tbody>
            {files.map((file, i) => (
              <tr key={file.path}>
                <td>{full ? <a href={`#file-${i + 1}`}>{file.path}</a> : file.path}</td>
                <td>{file.size}</td>
                <td><code>{file.sha256}</code></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {full && files.map((file, i) => (
        <details key={file.path} id={`file-${i + 1}`} className="pack-file">
          <summary><code>{file.path}</code> <span className="muted">{file.size} bytes</span></summary>
          {file.text != null
            ? <pre className="file" data-path={file.path}>{file.text}</pre>
            : <p className="muted">Binary file, shown by size and SHA-256 only.</p>}
        </details>
      ))}
    </>
  );
}

export function DeploymentBadge({ deployment }: { deployment: Schemas["Deployment"] }) {
  return <Badge kind={deployment.verification}>{deployment.label}</Badge>;
}

/** An application's latest Tested or Published release (catalog card). */
export function TestedReleaseLine({ release }: { release: Schemas["TestedRelease"] }) {
  return (
    <div className="tested-release">
      <span>Tested release <Link to={`/releases/${release.id}`}>{release.version}</Link></span>
      <Badge kind={release.state}>{release.state.charAt(0).toUpperCase() + release.state.slice(1)}</Badge>
      <span className="muted">{release.counts.pass} pass · {release.counts.fail} fail · {release.counts["not-run"]} not run · {release.platform}</span>
      <span className="actions">
        {release.deployment && (
          <>
            <a className="button" href={release.deployment.url}>Open demo</a>
            <DeploymentBadge deployment={release.deployment} />
          </>
        )}
        <a className="button secondary" href={`/api/v1/releases/${release.id}/download`}>Download tested release</a>
      </span>
    </div>
  );
}

const SEVERITY_BADGE: Record<string, string> = { critical: "failed", important: "discovered", nice: "packaged" };

/** A package's completeness against the spec (docs/PACKAGE-COMPLETENESS.md): the score per
 * section, then every gap with why it matters and the page and action that fix it. */
export function CompletenessChecklist({ completeness, fixLinks }: { completeness: Schemas["PackageCompleteness"]; fixLinks?: boolean }) {
  const c = completeness;
  const { critical, important, nice } = c.counts;
  return (
    <>
      <p id="completeness-summary">
        <Badge kind={c.passed ? "pass" : "failed"}>{c.passed ? "Complete" : "Critical items missing"}</Badge>{" "}
        <strong>{c.score}%</strong> of what a builder needs for a {c.surface} application
        {c.gaps.length ? ` · missing: ${critical} critical, ${important} important, ${nice} nice to have` : " · nothing missing"}.
      </p>
      <div className="scroll">
        <table className="flow" aria-label="Completeness by section">
          <thead><tr><th scope="col">Section</th><th scope="col">Score</th><th scope="col">Present</th><th scope="col">Missing</th></tr></thead>
          <tbody>{c.sections.filter((s) => s.present.length + s.missing.length > 0).map((s) => (
            <tr key={s.id}><td>{s.title}</td><td>{s.score}%</td><td>{s.present.length}</td><td>{s.missing.length}</td></tr>
          ))}</tbody>
        </table>
      </div>
      {c.gaps.length > 0 && (
        <ul className="checks" id="completeness-gaps" aria-label="Missing items">
          {c.gaps.map((g) => (
            <li key={g.item} className={g.severity === "critical" ? "failed" : undefined} data-item={g.item}>
              <Badge kind={SEVERITY_BADGE[g.severity]}>{g.severity}</Badge> <strong>{g.title}</strong>. {g.why}
              {g.details.length > 0 && <ul>{g.details.map((d) => <li key={d}>{d}</li>)}</ul>}
              <div>
                Fix: {fixLinks ? <Link to={g.fix.href}>{g.fix.page}</Link> : g.fix.page}: {g.fix.action}
                {g.capture_gap && <span className="muted"> (Not recorded today: {g.capture_gap})</span>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
