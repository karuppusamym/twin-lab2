// Colour contrast of the design tokens, light and dark, against WCAG 2.2 AA: 4.5:1 for text,
// 3:1 for the parts of a control and the focus ring. jsdom cannot compute layout colours, so
// axe's color-contrast rule does not run in the page tests; this checks every pairing the
// stylesheet uses instead (axe ran on the live demo in both themes too).
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(resolve(__dirname, "../src/styles.css"), "utf8");

/** The `--name: value` declarations of the first block that follows `marker`. */
function tokens(marker: string): Record<string, string> {
  const start = css.indexOf(marker);
  if (start < 0) throw new Error(`no ${marker} in styles.css`);
  const open = css.indexOf("{", css.indexOf(":root", start));
  const close = css.indexOf("}", open);
  const out: Record<string, string> = {};
  for (const [, name, value] of css.slice(open + 1, close).matchAll(/--([\w-]+):\s*([^;]+);/g)) if (name && value) out[name] = value.trim();
  return out;
}

const light = tokens("/* -- design tokens");
const surfaces = tokens("/* -- surfaces");
const dark = { ...light, ...tokens("@media (prefers-color-scheme: dark)") };

function rgb(hex: string): [number, number, number] {
  const h = hex.replace("#", "");
  const full = h.length === 3 ? [...h].map((c) => c + c).join("") : h;
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16)) as [number, number, number];
}

function luminance(hex: string) {
  const [r, g, b] = rgb(hex).map((c) => {
    const v = c / 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function ratio(a: string, b: string) {
  const [la, lb] = [luminance(a), luminance(b)];
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

const WHITE = "#ffffff";
type Pair = [what: string, fg: string, bg: string, min: number];

/** Every foreground/background pairing in styles.css, by token name (or a literal colour). */
const PAIRS: Pair[] = [
  ["body text on the page", "color-text", "color-bg", 4.5],
  ["body text on a card", "color-text", "color-surface", 4.5],
  ["muted text on the page", "color-muted", "color-bg", 4.5],
  ["muted text on a card", "color-muted", "color-surface", 4.5],
  ["links on the page", "color-link", "color-bg", 4.5],
  ["links on a card", "color-link", "color-surface", 4.5],
  ["links in the next-step card", "color-link", "color-callout-bg", 4.5],
  ["text in the next-step card", "color-text", "color-callout-bg", 4.5],
  ["next-step label", "active-text", "color-callout-bg", 4.5],
  ["header links", "color-header-link", "color-header", 4.5],
  ["header brand", WHITE, "color-header", 4.5],
  ["signed-in user", "color-header-muted", "color-header", 4.5],
  ["primary button", "color-primary-text", "color-primary", 4.5],
  ["secondary button", "color-secondary-text", "color-secondary-bg", 4.5],
  ["secondary button in the header", "color-secondary-text", "color-secondary-bg", 4.5],
  ["destructive button", "color-destructive", "color-surface", 4.5],
  ["destructive button, hovered", "color-destructive-text", "color-destructive", 4.5],
  ["active badge", "active-text", "active-bg", 4.5],
  ["done badge", "done-text", "done-bg", 4.5],
  ["review badge", "review-text", "review-bg", 4.5],
  ["failed badge", "failed-text", "failed-bg", 4.5],
  ["neutral badge", "neutral-text", "neutral-bg", 4.5],
  ["selected table row", "color-text", "active-bg", 4.5],
  ["warning", "color-text", "review-bg", 4.5],
  ["unsaved-changes note", "review-text", "review-bg", 4.5],
  ["blocked note", "failed-text", "failed-bg", 4.5],
  ["blocked text on a card", "failed-text", "color-surface", 4.5],
  ["error text on a card", "failed-text", "color-surface", 4.5],
  ["notice on a card", "done-text", "color-surface", 4.5],
  ["timeline step", "color-text", "color-secondary-bg", 4.5],
  ["timeline presence step", "color-muted", "neutral-bg", 4.5],
  ["code block", "color-text", "color-bg", 4.5],
  ["progress tick, done", "done-text", "done-bg", 4.5],
  ["progress tick, next", "color-primary-text", "color-primary", 4.5],
  ["progress tick, to do", "color-muted", "neutral-bg", 4.5],
  // non-text: a control's boundary and the focus ring (WCAG 1.4.11)
  ["input border on a card", "color-input-border", "color-surface", 3],
  ["primary button on a card", "color-primary", "color-surface", 3],
  ["focus ring on the page", "focus-color", "color-bg", 3],
  ["focus ring on a card", "focus-color", "color-surface", 3],
  ["focus ring in the header", "focus-color-on-dark", "color-header", 3],
  ["lane bar border", "done-strong", "color-surface", 3],
  ["missing lane bar border", "failed-strong", "color-surface", 3],
];

function colour(theme: Record<string, string>, name: string) {
  if (name.startsWith("#")) return name;
  const value = theme[name];
  if (!value?.startsWith("#")) throw new Error(`--${name} is not a hex colour: ${value}`);
  return value;
}

describe.each([["light", light], ["dark", dark]] as const)("%s theme", (_name, theme) => {
  it.each(PAIRS)("%s", (what, fg, bg, min) => {
    const r = ratio(colour(theme, fg), colour(theme, bg));
    expect(r, `${what}: --${fg} on --${bg} is ${r.toFixed(2)}:1, needs ${min}:1`).toBeGreaterThanOrEqual(min);
  });
});

describe("surface chips", () => {
  it.each(Object.entries(surfaces))("white text on --%s reaches 4.5:1", (_name, value) => {
    expect(ratio(WHITE, value)).toBeGreaterThanOrEqual(4.5);
  });
});
