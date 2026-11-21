/** "2026-11-18T09:30:00+00:00" → "2026-11-18 09:30". */
export function when(value: string | null | undefined, length = 16): string {
  if (!value) return "";
  return value.slice(0, length).replace("T", " ");
}

/** The first 12 characters of a SHA or revision. */
export function short(value: string | null | undefined, length = 12): string {
  return (value ?? "").slice(0, length);
}

/** 7.0 → "7", 2.5 → "2.5", null → "". */
export function amount(value: number | null | undefined): string {
  if (value === null || value === undefined) return "";
  return Number.isInteger(value) ? String(value) : String(Number(value.toPrecision(4)));
}

export function plural(count: number, word: string, many = `${word}s`) {
  return `${count} ${count === 1 ? word : many}`;
}

/** 45 → "45 s", 201 → "3 min 21 s", 3720 → "1 h 2 min". */
export function duration(seconds: number) {
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min` + (s % 60 ? ` ${s % 60} s` : "");
  const m = Math.floor((s % 3600) / 60);
  return `${Math.floor(s / 3600)} h` + (m ? ` ${m} min` : "");
}

export function capitalize(value: string) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

/** "waiting_for_answer" → "waiting for answer". */
export function words(value: string) {
  return value.replace(/_/g, " ");
}

/** A release as people say it: "Billing 1.2 (7c41e9b)". The full commit stays on the release page. */
export function releaseName(app: string | null | undefined, release: { label: string; commit_sha: string }) {
  return `${app ? `${app} ` : ""}${release.label} (${release.commit_sha.slice(0, 7)})`;
}

/** The GitHub page of a commit, when the repository is a GitHub "owner/name". */
export function commitUrl(repository: string | null | undefined, sha: string | null | undefined) {
  return repository && sha && /^[\w.-]+\/[\w.-]+$/.test(repository) ? `https://github.com/${repository}/commit/${sha}` : null;
}
