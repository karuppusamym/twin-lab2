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

export function capitalize(value: string) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

/** "waiting_for_answer" → "waiting for answer". */
export function words(value: string) {
  return value.replace(/_/g, " ");
}
