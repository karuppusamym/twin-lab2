# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Where a value a person typed is taken out of what the recorder writes, and where it is not.

The recorder (twinlab2_recorder.web.recorder._Secrets) holds every typed value in memory and never writes one.
Snapshots, steps, locators, tab addresses, storage keys and the HAR still show what the
application did with those values, so each is taken out again before anything is written. Where
it is taken out is decided by what the place holds:

- a value: text nodes, form field values (`value`), attributes that carry content (`title`,
  `alt`, `placeholder`, `aria-label` and the other attributes of no structural meaning, `data-*`),
  the query and fragment of an address and a path segment that is the whole value, a JSON
  string, a form-encoded field. There the typed value is replaced by its shape (letters A,
  digits 9) wherever it is a whole token: not inside a longer word or identifier (`report`
  typed leaves `Reports`, `main:navReports` and `report-row` as they are). In `data-*`
  attributes a token joined to a word by `-`, `:` or `.` is part of an identifier and kept;
  a value shorter than 3 characters is taken out only where it is the whole text;
- structure: `id`, `class`, `name`, `for`, `form`, `list`, `headers`, the ARIA id references,
  test ids, `slot`, `part`: never changed by a typed value inside them. One whose whole value
  (for a list such as `class`, one whole token) is a typed value is a privacy question all the
  same: it becomes the placeholder `tl-typed-a` (`tl-typed-b`, ... one per typed value), in the
  snapshot, in the step's selector and locators, and in a page's code and style sheets where
  they name it, so the rebuilt page, the package and the replay agree on it. A step that uses
  one says so in `typed_placeholders` (the placeholder and the step the value was typed at);
- enumerated attributes (`type`, `role`, `method`, `tabindex`, ...): never changed;
- addresses (`href`, `src`, `action`, ...): the path structure is kept, a path segment that is
  the whole value, the query values and the fragment are values (a fragment that names a
  replaced id gets its placeholder);
- code (`<script>`, `<style>`, `on*` and `style` attributes, `javascript:` addresses): a typed
  value that stands alone (not joined to an identifier) is replaced by its placeholder, so a
  selector or an id reference in the code keeps naming the element.

Pictures are masked by the same token rule in the browser (twinlab2_recorder.web.recorder.MARK_TYPED_JS).
"""

from __future__ import annotations

import html as html_mod
import json
import re
from functools import lru_cache
from urllib.parse import quote, quote_plus, unquote, urlsplit, urlunsplit

PLACEHOLDER = "tl-typed-"
SHORT = 3  # a value shorter than this is taken out only where it is the whole text

TESTID_ATTRS = frozenset({"data-testid", "data-test-id", "data-test", "data-cy", "data-qa"})
# Whitespace-separated lists of names or id references: each token is compared on its own.
TOKEN_LIST_ATTRS = frozenset({
    "class", "headers", "aria-controls", "aria-labelledby", "aria-describedby", "aria-owns",
    "aria-flowto", "aria-details", "itemref", "part",
})  # fmt: skip
# Names and ids: kept as they are unless the whole value is a typed value (then the placeholder).
IDENTIFIER_ATTRS = TOKEN_LIST_ATTRS | TESTID_ATTRS | frozenset({
    "id", "name", "for", "form", "list", "aria-activedescendant", "aria-errormessage", "slot",
    "itemid", "itemprop", "contextmenu", "popovertarget", "anchor", "commandfor",
})  # fmt: skip
# Values from a closed vocabulary the application chose: never a typed value's place.
ENUM_ATTRS = frozenset({
    "type", "role", "method", "enctype", "target", "rel", "dir", "lang", "xml:lang", "tabindex",
    "colspan", "rowspan", "scope", "autocomplete", "autocapitalize", "inputmode", "enterkeyhint",
    "charset", "http-equiv", "align", "valign", "shape", "wrap", "spellcheck", "draggable",
    "contenteditable", "translate", "hidden", "disabled", "checked", "selected", "readonly",
    "required", "multiple", "autofocus", "novalidate", "formnovalidate", "maxlength",
    "minlength", "size", "cols", "rows", "min", "max", "step", "width", "height", "accept",
    "crossorigin", "integrity", "nonce", "referrerpolicy", "loading", "decoding", "sizes",
    "media", "async", "defer", "nomodule", "open", "sandbox", "allow", "frameborder",
    "scrolling", "start", "reversed", "span", "xmlns", "version", "viewbox", "d", "points",
    "transform", "fill", "stroke", "stroke-width", "opacity", "x", "y", "x1", "y1", "x2", "y2",
    "cx", "cy", "r", "rx", "ry", "offset", "preserveaspectratio", "focusable", "popover",
    "aria-expanded", "aria-selected", "aria-hidden", "aria-checked", "aria-pressed",
    "aria-disabled", "aria-live", "aria-atomic", "aria-busy", "aria-level", "aria-sort",
    "aria-haspopup", "aria-modal", "aria-multiselectable", "aria-orientation", "aria-readonly",
    "aria-required", "aria-invalid", "aria-current", "aria-autocomplete", "aria-posinset",
    "aria-setsize", "aria-colcount", "aria-colindex", "aria-rowcount", "aria-rowindex",
    "aria-relevant", "aria-valuemin", "aria-valuemax", "aria-multiline",
})  # fmt: skip
URL_ATTRS = frozenset({
    "href", "src", "action", "formaction", "poster", "cite", "data", "background", "ping",
    "xlink:href", "longdesc", "manifest", "codebase", "usemap", "srcset", "imagesrcset",
})  # fmt: skip

_WORD_START = re.compile(r"^\w")
_WORD_END = re.compile(r"\w$")
# Where a typed value is a whole token. "prose": not inside a word. "ident": also not joined to
# a word by - : . (an identifier such as main:report-row). "style": not joined by - (a class
# `nav-report`) or an escaped colon; a `:` after it is a pseudo-class. "script": like ident.
_BEFORE = {
    "prose": r"(?<!\w)",
    "ident": r"(?<!\w)(?<!\w[-:.])(?<!\\[:.])",
    "style": r"(?<!\w)(?<!\w-)(?<!\\:)",
    "script": r"(?<!\w)(?<!\w[-:.])(?<!\\[:.])",
}
_AFTER = {
    "prose": r"(?!\w)",
    "ident": r"(?!\w)(?![-:.]\w)",
    "style": r"(?!\w)(?!-\w)(?!\\:)",
    "script": r"(?!\w)(?![-:.]\w)",
}
# A tag, quote-aware (an attribute value may hold ">").
_TAG = re.compile(r"""<[A-Za-z][^\s/>"']*(?:"[^"]*"|'[^']*'|[^'">])*>""")
_ATTR = re.compile(r"""(?<=[\s"'])([^\s"'>/=]+)(\s*=\s*)("[^"]*"|'[^']*'|[^\s"'=<>`]+)""")
_HTMLISH = re.compile(r"<(?:[A-Za-z][\w:-]*[\s>/]|!--|!\[CDATA\[|\?xml)")
_URLISH = re.compile(r"(?:https?|wss?)://\S*")
_CSS = re.compile(
    r"""\[\s*([^\s~|^$*!=\]]+)\s*([~|^$*]?=)\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s\]]+)"""
    r"""\s*([iIsS])?\s*\]"""
    r"""|("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')"""
    r"""|([#.])((?:\\[0-9a-fA-F]{1,6}\s?|\\.|[\w-]|[^\x00-\x7f])+)"""
)


def key(text: str) -> str:
    """How two values are compared: entities read, spaces collapsed, case ignored."""
    return " ".join(html_mod.unescape(text or "").split()).lower()


def placeholder_name(n: int) -> str:
    """The n-th placeholder (0-based): tl-typed-a, ..., tl-typed-z, tl-typed-aa, ... (letters
    only, so no digit masking changes it)."""
    out = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(97 + r) + out
    return PLACEHOLDER + out


def _css_unescape(text: str) -> str:
    return re.sub(
        r"\\([0-9a-fA-F]{1,6})\s?|\\(.)",
        lambda m: chr(int(m.group(1), 16)) if m.group(1) else m.group(2),
        text,
    )


@lru_cache(maxsize=4096)
def _pattern(form: str, mode: str) -> re.Pattern:
    words = form.split()
    body = r"\s+".join(re.escape(w) for w in words)
    pre = _BEFORE[mode] if _WORD_START.search(words[0]) else ""
    post = _AFTER[mode] if _WORD_END.search(words[-1]) else ""
    return re.compile(pre + body + post, re.I)


def _shape(text: str) -> str:
    from twinlab2_contract.evidence.dom import shape

    return shape(text) or ""


class TypedValues:
    """The rule above, for the values typed so far. `values` holds (value, origin, type event
    id); `steps` maps a type event id to the step number it was typed at."""

    def __init__(self) -> None:
        self.values: list[tuple[str, str, str]] = []
        self.steps: dict[str, int] = {}
        self.placeholders: dict[str, tuple[str, str]] = {}  # key -> (placeholder, tid)

    @classmethod
    def for_replay(cls, typed: dict[str, str]) -> TypedValues:
        """The rule for a replay: placeholder -> the value the replay typed for it. A live page
        then has the recorded placeholders where the recording has them, so the two compare."""
        out = cls()
        for ph, value in typed.items():
            if isinstance(value, str) and value.strip():
                out.values.append((value, "", ""))
                out.placeholders.setdefault(key(value), (ph, ""))
        return out

    def _remember(self, value: str, tid: str) -> None:
        k = key(value)
        if k and k not in self.placeholders:
            self.placeholders[k] = (placeholder_name(len(self.placeholders)), tid)

    # --- the forms a value takes -----------------------------------------------------------

    def _forms(self, har: bool = False) -> list[tuple[str, bool]]:
        """Each value in the forms it can take (in markup, or also URL-encoded and JSON-escaped
        in the HAR), longest first; True when long enough to be taken out wherever it is a
        whole token (a short one only where it is the whole text)."""
        seen: dict[str, bool] = {}
        for value, _, _ in self.values:
            v = value.strip()
            forms = {v, html_mod.escape(v)}
            if har:
                forms |= {quote(v), quote_plus(v), json.dumps(v)[1:-1]}
            for f in forms:
                if f.strip():
                    seen[f] = len(f.strip()) >= SHORT
        return sorted(seen.items(), key=lambda kv: -len(kv[0]))

    def _ph(self, text: str, used: set | None) -> str | None:
        """The placeholder of a typed value that is this whole text, or None."""
        hit = self.placeholders.get(key(text))
        if hit is None:
            return None
        if used is not None:
            used.add(hit[0])
        return hit[0]

    # --- values: replaced by their shape where they are a whole token ----------------------

    def _text(self, text: str, mode: str = "prose", har: bool = False) -> str:
        if not self.values or not text or not text.strip():
            return text
        for form, everywhere in self._forms(har):
            if everywhere:
                text = _pattern(form, mode).sub(lambda m: _shape(m.group()), text)
            elif text.strip().lower() == form.strip().lower():
                text = text.replace(text.strip(), _shape(text.strip()))
            elif har:  # a short value: a whole JSON string or form-encoded field only
                f = re.escape(form.strip())
                text = re.sub(r'(?<=")' + f + r'(?=")', lambda m: _shape(m.group()), text)
                text = re.sub(r"(?<==)" + f + r"(?=&|$)", lambda m: _shape(m.group()), text)
        return text

    def redact_text(self, text: str) -> str:
        """Typed values out of a text (a dialog's message, a storage key): whole tokens only."""
        return self._text(text)

    def contains(self, text: str) -> bool:
        """Whether a text shows a typed value as a whole token (a short one as the whole text)."""
        if not self.values or not isinstance(text, str):
            return False
        for form, everywhere in self._forms():
            if everywhere:
                if _pattern(form, "prose").search(text):
                    return True
            elif text.strip().lower() == form.strip().lower():
                return True
        return False

    # --- code: a typed value standing alone becomes its placeholder -------------------------

    def _code(self, text: str, mode: str, used: set | None) -> str:
        if not self.values or not text:
            return text
        for value, _, _ in sorted(self.values, key=lambda v: -len(v[0].strip())):
            ph = self.placeholders.get(key(value), (None,))[0]
            if ph is None:
                continue
            for form in {value.strip(), html_mod.escape(value.strip())}:
                if not form:
                    continue
                if len(form) >= SHORT:
                    pat = _pattern(form, mode)
                else:
                    pat = re.compile(r"(?<=[\"'])" + re.escape(form) + r"(?=[\"'])", re.I)
                new = pat.sub(lambda _m, p=ph: p, text)
                if new != text and used is not None:
                    used.add(ph)
                text = new
        return text

    # --- addresses -----------------------------------------------------------------------------

    def redact_url(self, url: str, markup: bool = False, used: set | None = None) -> str:
        """Typed values out of an address: the path structure stays, a path segment that is a
        whole typed value is replaced by its shape (a word typed somewhere must not change the
        address of a page that happens to contain it), the query and the fragment are values.
        In markup a fragment that is a whole typed value names an element whose id became the
        placeholder, and gets it too."""
        if not self.values or not url:
            return url
        low = url.strip().lower()
        if low.startswith("javascript:"):
            return self._code(url, "script", used)
        try:
            p = urlsplit(url)
        except ValueError:
            return self._text(url, har=True)
        if p.scheme and p.scheme.lower() not in ("http", "https", "ws", "wss", "file", "ftp"):
            return self._text(url, har=True)  # mailto:, tel:, data: and others: all value
        wanted = {key(v) for v, _, _ in self.values}
        segs = [_shape(unquote(s)) if key(unquote(s)) in wanted else s for s in p.path.split("/")]
        query = self._text(p.query, har=True) if p.query else p.query
        frag = p.fragment
        if frag:
            ph = self._ph(unquote(frag), used) if markup else None
            frag = ph or self._text(frag, har=True)
        path = "/".join(segs)
        if (path, query, frag) == (p.path, p.query, p.fragment):
            return url  # nothing typed in it: as written (an empty "#" or "?" stays)
        return urlunsplit((p.scheme, p.netloc, path, query, frag))

    # --- markup --------------------------------------------------------------------------------

    def redact_attr(self, name: str, value: str, used: set | None = None) -> str:
        """One attribute's value (as written in markup) with typed values out, by the rule."""
        n = name.lower()
        if not self.values or not value or not value.strip() or n in ENUM_ATTRS:
            return value
        if n in TOKEN_LIST_ATTRS:
            return re.sub(r"\S+", lambda m: self._ph(m.group(), used) or m.group(), value)
        if n in IDENTIFIER_ATTRS:
            return self._ph(value, used) or value
        if n.startswith("on"):
            return self._code(value, "script", used)
        if n == "style":
            return self._code(value, "style", used)
        if n in ("srcset", "imagesrcset"):
            return ",".join(
                re.sub(r"\S+", lambda m: self.redact_url(m.group(), True, used), part, count=1)
                for part in value.split(",")
            )
        if n in URL_ATTRS:
            return self.redact_url(value, markup=True, used=used)
        if n.startswith("data-"):
            return self._text(value, "ident")
        return self._text(value)

    def _tag(self, tag: str, used: set | None) -> str:
        def one(m: re.Match) -> str:
            raw = m.group(3)
            q = raw[0] if raw[0] in "\"'" else ""
            inner = raw[1:-1] if q else raw
            return m.group(1) + m.group(2) + q + self.redact_attr(m.group(1), inner, used) + q

        return _ATTR.sub(one, tag)

    def redact_html(self, markup: str, used: set | None = None) -> str:
        """Typed values out of a page's markup by the rule (tag and attribute names untouched).
        `used` collects the placeholders written."""
        if not self.values or not markup:
            return markup
        out: list[str] = []
        pos, n = 0, len(markup)
        while pos < n:
            lt = markup.find("<", pos)
            if lt < 0:
                out.append(self._text(markup[pos:]))
                break
            out.append(self._text(markup[pos:lt]))
            if markup.startswith("<!--", lt):
                end = markup.find("-->", lt + 4)
                end = n if end < 0 else end
                out.append("<!--" + self._text(markup[lt + 4 : end]) + markup[end : end + 3])
                pos = end + 3
                continue
            if markup.startswith("<![CDATA[", lt):
                end = markup.find("]]>", lt + 9)
                end = n if end < 0 else end
                out.append("<![CDATA[" + self.redact_html(markup[lt + 9 : end], used)
                           + markup[end : end + 3])  # fmt: skip
                pos = end + 3
                continue
            if markup.startswith(("</", "<!", "<?"), lt):
                gt = markup.find(">", lt)
                gt = n - 1 if gt < 0 else gt
                out.append(markup[lt : gt + 1])
                pos = gt + 1
                continue
            m = _TAG.match(markup, lt)
            if not m:  # a "<" in text
                out.append("<")
                pos = lt + 1
                continue
            tag = m.group()
            out.append(self._tag(tag, used))
            pos = m.end()
            name = re.match(r"<([^\s/>]+)", tag).group(1).lower()
            if name in ("script", "style") and not tag.endswith("/>"):
                close = re.compile(r"</" + name + r"\s*>", re.I).search(markup, pos)
                end = close.start() if close else n
                out.append(self._code(markup[pos:end], name, used))
                pos = end
        return "".join(out)

    # --- selectors -----------------------------------------------------------------------------

    def redact_css(self, css: str, used: set | None = None) -> tuple[str, bool]:
        """A CSS selector with the placeholder for an id, class or other name that is a whole
        typed value (the snapshot has it so); a content attribute's value and a quoted text by
        the value rule. Also True when a text in it (not a name) showed a typed value."""
        if not self.values or not css:
            return css, False
        shown = [False]

        def one(m: re.Match) -> str:
            if m.group(1) is not None:  # [attr op value flags]
                attr, op, raw = m.group(1), m.group(2), m.group(3)
                q = raw[0] if raw[0] in "\"'" else ""
                val = _css_unescape(raw[1:-1] if q else raw)
                n = attr.lower()
                if n in ENUM_ATTRS:
                    return m.group()
                if n in IDENTIFIER_ATTRS:
                    ph = self._ph(val, used) if op in ("=", "~=") else None
                    if ph is None:
                        return m.group()
                    new = ph
                else:
                    new = self._text(val, "ident" if n.startswith("data-") else "prose")
                    if new == val:
                        return m.group()
                flags = f" {m.group(4)}" if m.group(4) else ""
                return f"[{attr}{op}{json.dumps(new)}{flags}]"
            if m.group(5) is not None:  # a quoted text (:text-is("..."), :has-text(...))
                raw = m.group(5)
                inner = raw[1:-1]
                new = self._text(inner)
                if new == inner:
                    return raw
                shown[0] = True
                return json.dumps(new)
            ph = self._ph(_css_unescape(m.group(7)), used)
            return m.group(6) + ph if ph else m.group()

        return _CSS.sub(one, css), shown[0]

    # --- the HAR -------------------------------------------------------------------------------

    def scrub(self, value):
        """Parsed JSON (the HAR, a body) with typed values taken out of every string by the
        rule: a page's markup as markup, an address as an address, JSON in a string as JSON,
        any other string as a value (also URL-encoded)."""
        if isinstance(value, dict):
            return {k: self.scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub(v) for v in value]
        if not isinstance(value, str) or not self.values or not value:
            return value
        return self._scrub_str(value)

    def _scrub_str(self, s: str) -> str:
        st = s.strip()
        if st[:1] in "{[":
            try:
                doc = json.loads(st)
            except ValueError:
                doc = None
            if isinstance(doc, (dict, list)):
                new = self._json(doc)
                return s if new == doc else json.dumps(new, ensure_ascii=False,
                                                       separators=(",", ":"))  # fmt: skip
        if _URLISH.fullmatch(st):
            return self.redact_url(s)
        if _HTMLISH.search(s):
            return self.redact_html(s)
        return self._text(s, har=True)

    def _json(self, doc):
        if isinstance(doc, dict):
            return {(self._text(k, har=True) if isinstance(k, str) else k): self._json(v)
                    for k, v in doc.items()}  # fmt: skip
        if isinstance(doc, list):
            return [self._json(v) for v in doc]
        if isinstance(doc, str) and doc:
            return self._scrub_str(doc)
        if isinstance(doc, (int, float)) and not isinstance(doc, bool):
            # a typed number the application sent back as a JSON number: its shape, a number too
            # (a short one, such as a quantity of 3, is no value of its own: a count stays)
            text = json.dumps(doc)
            if len(text) >= SHORT and key(text) in self.placeholders:
                shape = _shape(json.dumps(doc))
                try:
                    return json.loads(shape)
                except ValueError:
                    return shape
        return doc

    # --- steps ---------------------------------------------------------------------------------

    TEXT_FIELDS = ("name", "text", "label")
    CSS_FIELDS = ("css", "rows", "control")

    def clean_locators(self, locs: list, used: set) -> list:
        """Locator candidates by the rule: one whose text (accessible name, label, placeholder,
        link text) shows a typed value is dropped; an id or test id that is a whole typed value
        gets its placeholder; selectors as `redact_css` leaves them (dropped when a text in one
        shows a typed value)."""
        out = []
        for c in locs:
            if not isinstance(c, dict):
                continue
            kind = c.get("kind")
            new, drop = dict(c), False
            for k, v in c.items():
                if not isinstance(v, str) or k == "kind":
                    continue
                if k in self.TEXT_FIELDS or (k == "text" and kind in ("label", "placeholder")):
                    drop = drop or self.contains(v)
                elif k == "value" and kind in ("id", "testid"):
                    new[k] = self._ph(v, used) or v
                elif k == "value":
                    drop = drop or self.contains(v)
                elif k in self.CSS_FIELDS:
                    new[k], shown = self.redact_css(v, used)
                    drop = drop or shown
            if not drop:
                out.append(new)
        return out

    def placeholder_notes(self, used: set) -> list[dict]:
        """What a step says about the placeholders it uses: each one and the step its value was
        typed at (never the value)."""
        by_ph = {ph: tid for ph, tid in self.placeholders.values()}
        return [{"placeholder": ph, "typed_at": self.steps.get(by_ph.get(ph, ""))}
                for ph in sorted(used) if ph in by_ph]  # fmt: skip
