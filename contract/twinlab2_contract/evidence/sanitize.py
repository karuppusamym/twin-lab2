# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Privacy for recorded evidence: drop credentials, reduce bodies to shapes, mask page text.

A recording of a real system holds customer data, cookies and tokens. Nothing recorded is kept as
a value. Headers that carry credentials are dropped as soon as a request is read; bodies become
shapes (field names and types); page snapshots keep their structure and shape-mask data:
`Smith` -> `Xxxxx`, `1001` -> `9999`.

Page text, two modes (`TWINLAB_PAGE_TEXT`, or `with page_text("strict"):`):

- **interface** (the default): the application's own interface text is kept verbatim (labels,
  legends, buttons, headings, table headers, options, link and menu text, titles, placeholders,
  validation and status messages), because a builder cannot rebuild what it cannot read. Data is
  masked: every text in a data cell (`td`, `dd`, a row header, `textarea`, `output`, `pre`,
  canvas text, anything editable), every field value, and inside interface text every token that
  has the shape of data (e-mail addresses, dates, times, decimals, any token with 3 or more
  digits), every Title-case word the same recording shows as data elsewhere (data cells, field
  values, API answers), every run of two or more Title-case words outside labels, buttons and
  headers (how names are written), and every token that differs between snapshots of the same
  screen at the same place. A message that mixes both keeps its words and masks its data
  (`Order ORD-1026 saved` -> `Order XXX-9999 saved`). Text of an element that holds a value
  (an element with an id or a value-like class that is not a label, heading, button, link or
  message) keeps only words the interface itself uses elsewhere. Format examples are kept
  (`name@example.test`, `ORD-0000`: reserved example domains, all-zero numbers).
  Four cases keep text the rules above would mask, because the page says it already or it is a
  label: a choice whose text repeats the code its own markup gives it (`<option value="open">
  Open`, a radio label `for="priority-low"`: `code_words`); in an element that holds a value,
  the words the page's own masked `<title>` shows (the application's name) and, beside
  interface words, the words that join them (`Signed in as Xxx`); a tooltip, `aria-label` or
  placeholder on a control, read as a label (a single word the data also shows stays, a name
  does not); a style sheet string without a word (an icon font's glyph).
- **strict**: every text, every text attribute and the style sheets are masked; only structure,
  ids, classes and names remain.

Values a person typed never reach this module: the recorder replaces them with their shape in
everything it writes, before any of this runs, where they are a value (text, form values,
content attributes, address queries) and as a whole token; ids, classes and names keep a typed
value inside them, and one that is a whole typed value is the placeholder `tl-typed-a`
(twinlab2_contract.evidence.typedvalues). It masks nothing else in the browser, so this rule is the only one for
page text. In interface mode style sheets are
kept (they are the application's code, not data), without `url(...)` addresses.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

PAGE_TEXT_MODES = ("interface", "strict")
_MODE: contextvars.ContextVar[str | None] = contextvars.ContextVar("page_text", default=None)


def page_text_mode() -> str:
    """The page-text mode in force: the one set with `page_text()`, else `TWINLAB_PAGE_TEXT`,
    else `interface`. A value that is not a known mode means strict (the safe reading)."""
    mode = (_MODE.get() or os.environ.get("TWINLAB_PAGE_TEXT") or "interface").strip().lower()
    return mode if mode in PAGE_TEXT_MODES else "strict"


@contextlib.contextmanager
def page_text(mode: str | None):
    """Run with a page-text mode (`interface` or `strict`); None keeps the one in force."""
    if mode is not None and mode not in PAGE_TEXT_MODES:
        raise ValueError(f"page text mode is one of {', '.join(PAGE_TEXT_MODES)}, not {mode!r}")
    token = _MODE.set(mode) if mode else None
    try:
        yield
    finally:
        if token is not None:
            _MODE.reset(token)


# --- headers ---------------------------------------------------------------------------------------

CREDENTIAL_HEADER = re.compile(
    r"^(cookie|set-cookie|set-cookie2|authorization|proxy-authorization|www-authenticate|"
    r"proxy-authenticate|x-.*-token|x-.*-key|x-api-key|x-auth.*|x-csrf.*|x-xsrf.*)$"
    r"|token|secret|passw|api[-_]?key|session|cookie|auth|credential|signature|csrf|xsrf|jwt",
    re.IGNORECASE,
)


def is_credential_header(name: str) -> bool:
    return bool(CREDENTIAL_HEADER.search(name.strip()))


def safe_headers(headers: list[dict] | None) -> dict[str, str]:
    """Headers without anything that could be a credential, lower-cased names."""
    out: dict[str, str] = {}
    for h in headers or []:
        name = str(h.get("name", "")).strip().lower()
        if name and not is_credential_header(name):
            out[name] = str(h.get("value", ""))
    return out


TECH_HEADERS = ("server", "x-powered-by", "x-aspnet-version", "x-aspnetmvc-version", "x-generator",
                "x-drupal-cache", "x-pingback", "x-runtime", "x-turbo-charged-by", "via")  # fmt: skip
PRODUCT_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]{0,39}(?:/\d[\w.]{0,15})?|\d+(?:\.\d+){1,3}")
COOKIE_NAME = re.compile(r"^[A-Za-z0-9_.!#$%&'*+^`|~-]{1,64}$")


def tech_headers(headers: list[dict] | None) -> dict[str, str]:
    """The response headers that name software (`Server`, `X-Powered-By`...), reduced to product
    tokens and versions: `Apache/2.4.41 (Ubuntu)` -> `Apache/2.4.41 Ubuntu`. Nothing else."""
    out: dict[str, str] = {}
    for h in headers or []:
        name = str(h.get("name", "")).strip().lower()
        if name not in TECH_HEADERS:
            continue
        value = str(h.get("value", ""))
        if name in ("x-aspnet-version", "x-aspnetmvc-version"):
            m = re.match(r"\s*(\d+(?:\.\d+){0,3})", value)
            out[name] = m.group(1) if m else "present"
        elif name == "x-pingback":
            out[name] = "present"  # an address: only that the header exists is kept
        else:
            out[name] = " ".join(PRODUCT_TOKEN.findall(value)[:6])
    return out


def cookie_names(entry: dict) -> set[str]:
    """The names of the cookies a request sent and a response set. Never a value."""
    names: set[str] = set()
    req, resp = entry.get("request") or {}, entry.get("response") or {}
    for side in (req, resp):
        for c in side.get("cookies") or []:
            names.add(str(c.get("name", "")))
        for h in side.get("headers") or []:
            hname = str(h.get("name", "")).lower()
            value = str(h.get("value", ""))
            if hname == "cookie":
                names.update(p.split("=", 1)[0].strip() for p in value.split(";") if "=" in p)
            elif hname == "set-cookie":
                names.update(v.split("=", 1)[0].strip() for v in value.splitlines() if "=" in v)
    return {n for n in names if COOKIE_NAME.match(n)}


COOKIE_FLAGS = {"httponly": "http_only", "secure": "secure", "partitioned": "partitioned"}


def _duration(seconds: int) -> str:
    """`3600` -> `1 hour (3600 s)`: how long a cookie lives, never a date."""
    for size, word in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size and seconds % size == 0:
            n = seconds // size
            return f"{n} {word}{'s' if n != 1 else ''} ({seconds} s)"
    return f"{seconds} s"


def _cookie_domain(domain: str, host: str) -> str:
    """Where a cookie's `Domain` points, relative to the application's own host. The host
    itself is never written: a real hostname is not carried."""
    d = domain.strip().lstrip(".").lower()
    h = host.split(":", 1)[0].lower()
    if not d:
        return "not set: the cookie belongs to the application's own host only"
    if d == h:
        return "the application's own host"
    if h.endswith("." + d):
        return "a parent domain of the application's host: shared with its sibling hosts"
    return "another domain than the application's host"


def cookie_attributes(entry: dict) -> list[dict]:
    """The cookies a response set, by name, with their attributes and NEVER their value: `Path`
    (as an address pattern), `Domain` relative to the application's host, `HttpOnly`, `Secure`,
    `SameSite`, `Partitioned`, and the lifetime as a duration (`Max-Age`, or `Expires` measured
    from the response's own date): a session cookie, a persistent one, or one being cleared.

    Read from the response's `Set-Cookie` headers; a HAR that holds only the parsed `cookies`
    list (some recorders) is read from that. The value part of each header is dropped unread."""
    from email.utils import parsedate_to_datetime

    req, resp = entry.get("request") or {}, entry.get("response") or {}
    host = urlsplit(str(req.get("url") or "")).netloc
    when = None
    for h in resp.get("headers") or []:
        if str(h.get("name", "")).lower() == "date":
            try:
                when = parsedate_to_datetime(str(h.get("value") or ""))
            except (TypeError, ValueError):
                when = None
    if when is None and entry.get("startedDateTime"):
        from datetime import datetime

        try:
            when = datetime.fromisoformat(str(entry["startedDateTime"]).replace("Z", "+00:00"))
        except ValueError:
            when = None

    def lifetime(max_age: str | None, expires: str | None) -> dict:
        seconds = None
        if max_age is not None and re.fullmatch(r"-?\d{1,12}", max_age.strip()):
            seconds = int(max_age)
        elif expires and when is not None:
            try:
                at = parsedate_to_datetime(expires)
            except (TypeError, ValueError):
                try:
                    from datetime import datetime

                    at = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                except ValueError:
                    at = None
            if at is not None and at.tzinfo is not None and when.tzinfo is not None:
                seconds = int((at - when).total_seconds())
        if max_age is None and not expires:
            return {"lifetime": "session: no Max-Age or Expires, it ends when the browser closes"}
        if seconds is None:
            return {"lifetime": "persistent: its expiry could not be read"}
        if seconds <= 0:
            return {"lifetime": "cleared: Max-Age 0 or an expiry in the past (the browser "
                                "deletes it)", "clears": True}  # fmt: skip
        return {"lifetime": f"persistent: {_duration(seconds)}", "max_age_seconds": seconds}

    out: dict[str, dict] = {}
    for h in resp.get("headers") or []:
        if str(h.get("name", "")).lower() != "set-cookie":
            continue
        for line in str(h.get("value") or "").splitlines():
            first, _, rest = line.partition(";")
            name = first.split("=", 1)[0].strip()
            if "=" not in first or not COOKIE_NAME.match(name):
                continue
            row: dict = {"name": name}
            attrs: dict[str, str | None] = {}
            for part in rest.split(";"):
                key, eq, val = part.partition("=")
                key = key.strip().lower()
                if key:
                    attrs[key] = val.strip() if eq else None
            row["path"] = route_pattern(attrs["path"]) if attrs.get("path") else "not set"
            row["domain"] = _cookie_domain(attrs.get("domain") or "", host)
            for flag, field in COOKIE_FLAGS.items():
                row[field] = flag in attrs
            same = (attrs.get("samesite") or "").capitalize()
            row["same_site"] = same if same in ("Lax", "Strict", "None") else "not set"
            row.update(lifetime(attrs.get("max-age"), attrs.get("expires")))
            out[name] = row
    for c in resp.get("cookies") or []:  # only when the headers did not hold the cookie
        name = str(c.get("name") or "")
        if name in out or not COOKIE_NAME.match(name):
            continue
        same = str(c.get("sameSite") or "").capitalize()
        row = {"name": name,
               "path": route_pattern(str(c["path"])) if c.get("path") else "not set",
               "domain": _cookie_domain(str(c.get("domain") or ""), host),
               "http_only": bool(c.get("httpOnly")), "secure": bool(c.get("secure")),
               "partitioned": False,
               "same_site": same if same in ("Lax", "Strict", "None") else "not set"}  # fmt: skip
        row.update(lifetime(None, str(c["expires"]) if c.get("expires") else None))
        out[name] = row
    return [out[n] for n in sorted(out)]


# --- values ----------------------------------------------------------------------------------------


def mask(text: str) -> str:
    """The shape of a value: letters to x/X, digits to 9, everything else kept."""
    return "".join(
        ("X" if c.isupper() else "x") if c.isalpha() else "9" if c.isdigit() else c for c in text
    )


IDENTIFIER_ATTRS = {"id", "class", "name", "for", "href", "action", "src", "aria-controls", "aria-labelledby",
                    "aria-describedby", "headers", "list", "form"}  # fmt: skip
DATA_TOKEN = re.compile(r"[A-Za-z0-9]*\d[A-Za-z0-9]*")


def mask_identifiers(value: str) -> str:
    """Mask record identifiers built into attribute values: any part with 3 or more digits.

    `loan-L-200040` -> `loan-L-999999`, `/reviews/R-1001` -> `/reviews/R-9999`. Short codes such
    as `CRD-01` are application constants and stay as they are.
    """
    return DATA_TOKEN.sub(
        lambda m: mask(m.group()) if sum(c.isdigit() for c in m.group()) >= 3 else m.group(), value
    )


# A class of a CSS utility library (`h-100`, `col-lg-4`, `mb-5`): a number up to 100 after a
# name is a size or a step of the style sheet, never a record's number.
UTILITY_CLASS = re.compile(r"[a-z][a-z0-9]*(?:-[a-z][a-z0-9]*)*-(?:\d{1,2}|100)")


def _mask_classes(value: str) -> str:
    return " ".join(
        c if UTILITY_CLASS.fullmatch(c) else mask_identifiers(c) for c in value.split(" ")
    )


def mask_attributes(attrs):
    out = []
    for k, v in attrs:
        name = k.lower()
        if v and name == "class":
            v = _mask_classes(v)
        elif v and (name in IDENTIFIER_ATTRS or name.startswith("data-")):
            v = mask_asset_url(v) if name in ASSET_ATTRS else mask_identifiers(v)
        out.append((k, v))
    return out


# --- static files of the application's own origin ---------------------------------------------------

ASSET_ATTRS = {"src", "href", "poster", "srcset"}
STATIC_FILE = re.compile(
    r"\.(png|gif|jpe?g|svg|webp|ico|avif|bmp|css|js|mjs|woff2?|ttf|otf|eot)$", re.IGNORECASE
)
# A folder with one of these names holds what users put there, not the interface.
UPLOAD_WORDS = {"upload", "uploads", "avatar", "avatars", "attachment", "attachments", "user",
                "users", "profile", "profiles", "media", "files", "file", "documents", "document",
                "photo", "photos", "download", "downloads", "userfiles", "tmp", "temp",
                "customers", "customer", "accounts", "account"}  # fmt: skip
# Words of a file name that say only whose file it is (`user_4711.png`): not a name of its own.
KEY_FILE_WORDS = UPLOAD_WORDS | {"img", "image", "images", "pic", "picture", "thumb", "thumbnail",
                                 "id", "item", "record", "doc", "scan", "logo", "icon"}  # fmt: skip


def static_file_name(path: str) -> bool:
    """Whether the last part of a path is a static file the application ships, named by a word
    (`blazemeter-favicon-512x512.png`, `Lumia_1520.jpg`): an image, style sheet, script or font
    whose name holds a word of three or more letters that is not only a key's prefix (`user`,
    `img`), outside a folder for user content. Such a name is the interface's (a builder needs
    it to carry the file), not a record's: `10023.png`, `user_4711.jpg`, a hash or a UUID are."""
    parts = path.split("?", 1)[0].split("#", 1)[0].rstrip("/").split("/")
    last = parts[-1]
    if not STATIC_FILE.search(last) or any(c in last for c in "@:,=;"):
        return False
    folders = {w for part in parts[:-1] for w in re.split(r"[^a-z]+", part.lower()) if w}
    if folders & UPLOAD_WORDS:
        return False
    stem = last.rsplit(".", 1)[0]
    if re.fullmatch(r"[0-9a-f-]{16,}", stem, re.IGNORECASE):
        return False  # a hash or a UUID
    named = [w for w in re.findall(r"[A-Za-z]{3,}", stem) if w.lower() not in KEY_FILE_WORDS]
    return bool(named)


def mask_asset_url(value: str) -> str:
    """An address in `src`/`href`: a relative address of a static file of the application's own
    origin keeps its file name (`static_file_name`); its folders, its query and any other address
    have record numbers masked as everywhere (`mask_identifiers`)."""
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or not static_file_name(parts.path):
        return mask_identifiers(value)
    head, _, last = parts.path.rpartition("/")
    path = (mask_identifiers(head) + "/" if _ else "") + last
    rest = value[len(parts.path) :]
    return path + mask_identifiers(rest)


ID_SEGMENT = re.compile(
    r"^(\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,}|"
    r"(?=[^/]*\d)[A-Za-z0-9][A-Za-z0-9._~%+-]*)$",
    re.IGNORECASE,
)
VERSION_SEGMENT = re.compile(r"^v\d+(\.\d+)*$", re.IGNORECASE)


def is_id_segment(segment: str) -> bool:
    """Whether a path segment is a value (a number, UUID, hash, code with digits, e-mail, key)."""
    if not segment or segment.startswith("{") or VERSION_SEGMENT.match(segment):
        return False
    if any(c in segment for c in "@:,=;"):  # jane@bank.com, 1234:close, a=b: never a route name
        return True
    if re.search(r"\.(js|css|html?|png|jpe?g|gif|svg|ico|woff2?|map|json)$", segment, re.I):
        return False
    return bool(ID_SEGMENT.match(segment))


def route_pattern(path: str) -> str:
    """`/reviews/R-1001` -> `/reviews/{id}`; a second value becomes `{id2}` and so on. A
    segment without digits that a learned screen template names (Twin Lab's `templates`: many
    addresses with the same page structure, `/p/blue-linen-shirt`, `/p/red-wool-scarf`) is a
    value too: `/p/{slug}`."""
    path = urlsplit(path).path or "/"
    segs = [seg.split(";", 1)[0] for seg in path.split("/")]  # matrix parameters are dropped
    ids = [is_id_segment(seg) for seg in segs]
    slugs = _slug_positions(segs, ids)
    out, n, m = [], 0, 0
    for seg, is_id, i in zip(segs, ids, range(len(segs)), strict=True):
        if i in slugs:
            m += 1
            out.append("{slug}" if m == 1 else f"{{slug{m}}}")
        elif is_id:
            n += 1
            out.append("{id}" if n == 1 else f"{{id{n}}}")
        else:
            out.append(seg)
    pattern = "/".join(out) or "/"
    return _ALIASES.get(pattern, pattern)


# Addresses that serve the same document as another (`/index.html` beside `/`), found by
# Twin Lab's `har.screen_aliases` and applied while a brief is generated (`with screen_aliases(m):`):
# one screen, whose other addresses the brief names.
_ALIASES: dict[str, str] = {}


@contextlib.contextmanager
def screen_aliases(mapping: dict[str, str] | None):
    """Within the block, `route_pattern` writes each alias as its screen's address."""
    global _ALIASES
    before = _ALIASES
    _ALIASES = dict(mapping or {})
    try:
        yield
    finally:
        _ALIASES = before


def aliases() -> dict[str, str]:
    """The aliases in force: alias pattern -> the screen's address."""
    return dict(_ALIASES)


# Screen templates learned from a recording (Twin Lab's templates.rules_for), applied while a brief
# is generated (`with template_rules(rules):`). A rule is a tuple of segments: a literal, "{}"
# (any value segment) or "*" (any segment: a value of the template, written `{slug}`).
_TEMPLATE_RULES: list[tuple[str, ...]] = []


@contextlib.contextmanager
def template_rules(rules):
    """Within the block, `route_pattern` also applies these learned templates."""
    global _TEMPLATE_RULES
    before = _TEMPLATE_RULES
    _TEMPLATE_RULES = [tuple(r) for r in rules or []]
    try:
        yield
    finally:
        _TEMPLATE_RULES = before


def slug_masked(path: str, mark: str = "*") -> str:
    """The address with each segment a learned template names written `mark` (`/p/blue-linen`
    -> `/p/*`), every other segment as it is: for patterns that write values their own way."""
    segs = [seg.split(";", 1)[0] for seg in (urlsplit(path).path or "/").split("/")]
    slugs = _slug_positions(segs, [is_id_segment(s) for s in segs])
    return "/".join(mark if i in slugs else s for i, s in enumerate(segs)) or "/"


def _slug_positions(segs: list[str], ids: list[bool]) -> set[int]:
    for rule in _TEMPLATE_RULES:
        if len(rule) != len(segs):
            continue
        hits = set()
        for i, (want, seg, is_id) in enumerate(zip(rule, segs, ids, strict=True)):
            if want == "*":
                if not seg:
                    break
                hits.add(i)
            elif want == "{}":
                if not is_id:
                    break
            elif want != seg or is_id:
                break
        else:
            return hits
    return set()


SAFE_WORD = re.compile(r"^([^\d@]*|\(?\d{1,3}[).,:;]?)$")  # no digits, or a small number


def words(value) -> set[str]:
    """The words in a value (strings in JSON, page text), to recognise them in messages."""
    out: set[str] = set()
    if isinstance(value, str):
        out.update(w.lower() for w in re.findall(r"[^\W\d_]{2,}", value))
    elif isinstance(value, dict):
        for k, v in value.items():
            out |= words(v)
            if not re.match(r"^[A-Za-z_][\w-]*$", str(k)):
                out |= words(str(k))
    elif isinstance(value, list):
        for v in value[:500]:
            out |= words(v)
    return out


PROSE_WORDS = 12  # a string of more words than this is prose (a description), not a name field


def names_in(value, prose: bool = True) -> set[str]:
    """The words a value writes with a capital (`Ann Hartley`, but not `no` or `NEW`), lower-case:
    how names show in data. Strings in JSON too. `prose=False` leaves out strings of more than
    `PROSE_WORDS` words: in a description every sentence and term starts with a capital
    (`the Touch Bar`), and a name the data holds is in a short value of its own as well."""
    out: set[str] = set()
    if isinstance(value, str):
        if prose or len(value.split()) <= PROSE_WORDS:
            out.update(w.lower() for w in re.findall(r"(?<![\w])[A-Z][a-z'’-]+", value))
    elif isinstance(value, dict):
        for v in value.values():
            out |= names_in(v, prose)
    elif isinstance(value, list):
        for v in value[:500]:
            out |= names_in(v, prose)
    return out


PHRASE_MAX = 80  # characters: a value longer than this is not shown whole as a title


def phrase(text: str) -> str:
    """A text as `phrases_in` keeps it: lower-case, spaces collapsed."""
    return " ".join(text.split()).lower()


def phrases_in(value) -> set[str]:
    """The short values of an answer that a page may show whole as a title or a link (`Samsung
    galaxy s6`, `HTC One M9`), as `phrase()` writes them: strings of two to `PROSE_WORDS` words up
    to `PHRASE_MAX` characters, and single words that mix letters and digits (`X200`). Such a text
    on a page is the record's value as a whole, lower-case words and short tokens included."""
    out: set[str] = set()
    if isinstance(value, str):
        p = phrase(value)
        n = len(p.split())
        if (
            p
            and len(p) <= PHRASE_MAX
            and re.search(r"[^\W\d_]", p)
            and (2 <= n <= PROSE_WORDS or (n == 1 and re.search(r"\d", p) and len(p) >= 3))
        ):
            out.add(p)
    elif isinstance(value, dict):
        for v in value.values():
            out |= phrases_in(v)
    elif isinstance(value, list):
        for v in value[:500]:
            out |= phrases_in(v)
    return out


EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
NAME_WORD = re.compile(r"^[A-Z][a-z'’-]+$")  # Title-case word: how names are written


def app_text(text: str, known: set[str] = frozenset(), names: bool = True) -> str:
    """Text the page presents as the application's own words (labels, headings, titles, options),
    with anything that looks like data masked:

    - e-mail addresses, and parts with 3 or more digits (record numbers);
    - what follows a colon in a label-value heading (`Customer: Jane Doe`);
    - Title-case words the same recording shows elsewhere as data;
    - with `names` (headings, titles, options, tooltips): any run of two or more Title-case words,
      which is how names and addresses are written ("Jane Doe", "Elm Street"). Button, label and
      column-header text is passed with `names=False`: that is where an app's own Title Case lives.
    """
    text = EMAIL.sub(lambda m: mask(m.group()), text)
    text = mask_identifiers(text)
    head, sep, tail = text.partition(":")
    if sep and tail.strip() and len(head.split()) <= 4 and not tail.strip().startswith("//"):
        text = head + sep + mask(tail)
    parts = re.split(r"(\s+)", text)
    bare = [re.sub(r"[^\w'’-]", "", p) for p in parts]
    title = [bool(NAME_WORD.match(b)) for b in bare]
    out = []
    for i, part in enumerate(parts):
        if part.isspace() or not part:
            out.append(part)
            continue
        neighbours = [j for j in (i - 2, i + 2) if 0 <= j < len(parts)]
        in_run = names and title[i] and any(title[j] for j in neighbours)
        seen = title[i] and bare[i].lower() in known
        out.append(mask(part) if in_run or seen else part)
    return "".join(out)


EXAMPLE_EMAIL = re.compile(
    r"@([\w-]+\.)*(example\.(com|net|org)|example|test|invalid|localhost)$", re.IGNORECASE
)
DATA_PATTERNS = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?)?"  # dates (and date-times)
    r"|\d{1,2}[/.]\d{1,2}[/.]\d{2,4}"
    r"|\d{1,2}:\d{2}(?::\d{2})?"  # times
    r"|\d+[.,]\d+"  # decimals, amounts
)
WORD = re.compile(r"[^\W\d_]{2,}")
# Words that join a sentence and are never a value: kept (written lower-case) in the text of an
# element that holds a value, when it also says words of the interface (`Signed in as Xxx`).
FUNCTION_WORDS = frozenset(
    "a an and are as at be by for from in into is of on or the to was were with".split()
)


def _example_token(token: str) -> bool:
    """A format example rather than data: its digits are all zeros (`ORD-0000`, `000-000`)."""
    digits = [c for c in token if c.isdigit()]
    return bool(digits) and set(digits) == {"0"}


def _mask_data_tokens(text: str) -> str:
    """Each word (between spaces) that is an e-mail address (but at a reserved example domain),
    or holds a date, a time, a decimal or 3 or more digits (but an all-zero example such as
    `ORD-0000`), to its shape, prefix and all: `ORD-1026` -> `XXX-9999`."""

    def one(part: str) -> str:
        if not any(c.isdigit() for c in part) and "@" not in part:
            return part
        email = EMAIL.search(part)
        if email and not EXAMPLE_EMAIL.search(email.group()):
            return mask(part)
        if _example_token(part):
            return part
        if DATA_PATTERNS.search(part) or sum(c.isdigit() for c in part) >= 3:
            return mask(part)
        return part

    return "".join(one(p) for p in re.split(r"(\s+)", text))


def interface_text(
    text: str,
    known: set[str] = frozenset(),
    vocab: set[str] = frozenset(),
    vary: set[str] = frozenset(),
    *,
    names: bool = True,
    holder: bool = False,
    label: bool = False,
) -> str:
    """Interface text with its data masked (the `interface` rule in the module docstring).

    `known`: words (lower-case) the recording shows as data; `vocab`: words the interface itself
    uses in labels, buttons, headers, options and menus; `vary`: tokens (lower-case) that differ
    between snapshots of the same screen at this place. `names`: mask runs of Title-case words
    (off for labels, buttons and headers, where an app's own Title Case lives). `holder`: the
    text of an element that holds a value: only words in `vocab` stay. `label`: a label, button
    or header: a word the data shows is masked there only next to another one (`Delete Jane
    Doe`), since a label's single word (`Customer no.`) is the app's own even when data has it.
    """
    text = _mask_data_tokens(text)
    head, sep, tail = text.partition(":")
    if (sep and tail.strip() and len(head.split()) <= 4 and not tail.strip().startswith("//")
            and (tail.strip()[:1].isupper() or tail.strip()[:1].isdigit())):  # fmt: skip
        # `Customer: Jane Doe`: what follows the label is the value, unless the interface says it
        tail = "".join(
            p if p.isspace() or not WORD.search(p) or _bare(p).lower() in vocab else mask(p)
            for p in re.split(r"(\s+)", tail)
        )
        text = head + sep + tail
    parts = re.split(r"(\s+)", text)
    bare = [_bare(p) for p in parts]
    # (a word masked already, `Xxxx`, is not a name next to which another one would be)
    title = [bool(NAME_WORD.match(b)) and not re.fullmatch(r"X+x*", b) for b in bare]
    # a holder that also says some of the interface's own words (`Signed in as Xxx`) keeps the
    # words that join them; one that holds only a value keeps nothing
    joins = holder and any(b.lower() in vocab for b in bare if WORD.search(b))
    out = []
    for i, part in enumerate(parts):
        if part.isspace() or not part:
            out.append(part)
            continue
        low = bare[i].lower()
        neighbours = [j for j in (i - 2, i + 2) if 0 <= j < len(parts)]
        run = names and title[i] and any(title[j] for j in neighbours)
        run = run and not all(bare[j].lower() in vocab for j in [i, *neighbours] if title[j])
        seen = title[i] and low in known and low not in vocab
        if seen and label:  # in a label: only next to another such word, or when it is all of it
            seen = any(title[j] and bare[j].lower() in known for j in neighbours) or not any(
                WORD.search(b) and b.lower() not in known for b in bare)  # fmt: skip
        if (
            (
                holder
                and WORD.search(part)
                and low not in vocab
                and not (joins and bare[i] in FUNCTION_WORDS)
            )
            or seen
            or run
            or (low and low in vary)
            or any(t.lower() in vary for t in re.findall(r"[\w@.+-]+", part))
        ):
            out.append(mask(part))
        else:
            out.append(part)
    return "".join(out)


def _bare(part: str) -> str:
    return re.sub(r"[^\w'’-]", "", part)


def interface_tokens(text: str) -> set[str]:
    """The tokens of a text as `interface_text` compares them between snapshots (lower-case)."""
    return {t.lower() for t in re.findall(r"[\w@.+-]+", text) if t.strip(".-+")}


def mask_message(text: str, values: set[str] = frozenset()) -> str:
    """An application message with any value-like word masked (`Case C-1001` -> `Case X-9999`).

    A word is masked when it holds digits or `@`, or when it is capitalised and was seen as a value
    somewhere in the same evidence (`values`, lower-case): names in messages. Numbers of up to
    three digits are kept: in a message they are usually limits ("at least 10 characters").
    """
    out = []
    for i, w in enumerate(re.split(r"(\s+)", text.strip())):
        bare = re.sub(r"[^\w]", "", w)
        named = i > 0 and bare[:1].isupper() and bare.lower() in values
        out.append(mask(w) if named or not SAFE_WORD.match(w) else w)
    return "".join(out)[:300]


# --- page snapshots --------------------------------------------------------------------------------

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
        "source", "track", "wbr"}  # fmt: skip
DROP_WITH_CONTENT = {"script", "template"}
KEEP_TEXT = {"label", "h1", "h2", "h3", "h4", "h5", "h6", "button", "th", "option", "optgroup",
             "title", "legend", "caption", "dt", "summary"}  # fmt: skip
# Inline tags that do not change whose text it is: `<button><span>Save</span></button>`.
PASS_THROUGH = {"span", "b", "strong", "i", "em", "small", "abbr", "u", "sup", "sub", "kbd", "mark",
                "bdi", "font", "nobr"}  # fmt: skip
INTERACTIVE = {"input", "select", "textarea", "button", "a", "form"}
BUTTON_TYPES = {"submit", "button", "reset"}
# Where records show: every text inside is data (interface mode masks it whole).
DATA_TAGS = {"td", "dd", "textarea", "output", "pre", "tl-canvas-text"}
# Words of these are the interface's own vocabulary (with `option` when it is not a name).
VOCAB_TAGS = {"label", "button", "th", "legend", "caption", "summary", "dt", "option", "optgroup"}
LABEL_TAGS = {"button", "label", "th", "legend", "dt", "summary", "caption"}
NEUTRAL = {"tl-frame", "tl-shadow"}  # Twin Lab's own wrappers: they change nothing about the text
MESSAGE = re.compile(
    r"error|alert|notice|message|msg|warn|success|info|hint|help|toast|flash|feedback|invalid|"
    r"status|result|empty|banner",
    re.IGNORECASE,
)
DATA_WORDS = {"value", "values", "note", "notes", "comment", "comments", "description", "desc",
              "body", "address", "name", "email", "mail", "phone", "amount", "total", "balance",
              "customer", "payer", "user", "owner", "author", "ref", "number", "no", "id", "code",
              "account", "iban", "card"}  # fmt: skip
NAV_WORDS = {"nav", "navbar", "menu", "menubar", "sidebar", "breadcrumb", "breadcrumbs", "tabs"}
# The classes a media player library gives the controls it draws (video.js, Plyr, MediaElement,
# JW Player). Their texts (`Play Video`, `Current Time`, `This is a modal window.`) are the
# library's own interface, the same in every application; it shows no record. Data-shaped
# tokens in them (a time, a number) are still masked.
PLAYER_CLASS = re.compile(
    r"^(video-js|vjs-[\w-]+|plyr(__[\w-]+)?|mejs__[\w-]+|mejs-[\w-]+|jw-[\w-]+)$"
)


def _player(classes: str) -> bool:
    return any(PLAYER_CLASS.match(c) for c in classes.split())


TEXT_ATTRS = ("title", "aria-label", "placeholder", "alt")
# Attributes a UI library (Bootstrap, jQuery plugins) acts on: their values point at other
# elements of the page (`#videoModal`, `.collapse`) or are its own settings (`modal`, a slide's
# index `0`). Ids and classes are kept as written, so a reference to them is no more than they are.
BEHAVIOUR_ATTR = re.compile(
    r"data-(?:bs-)?(?:target|parent|toggle|dismiss|slide|slide-to|ride|interval|backdrop|keyboard"
    r"|spy|offset|placement|trigger|container|focus|pause|wrap|touch|display|reference|boundary)"
)
ELEMENT_REF = re.compile(r"[#.][A-Za-z][\w-]{0,79}(?:\s*,\s*[#.][A-Za-z][\w-]{0,79})*")
SETTING = re.compile(r"[A-Za-z][\w-]{0,39}|\d{1,5}")
# A data-* attribute value that is a name in the page's code: lower-case, no spaces, no number.
DATA_NAME = re.compile(r"[a-z][a-z_.:-]{0,39}")


def _has_name_word(value: str, known: set[str]) -> bool:
    """Whether a lower-case name holds a word the recording shows as a name (`data-who="smith"`)."""
    return any(w in known for w in re.split(r"[_.:-]+", value) if w)


CODE_NAME = re.compile(r"[A-Za-z][A-Za-z_.:-]{0,39}")


def code_words(*codes: str) -> set[str]:
    """The words of the codes a page's own markup gives a choice (an option's `value`, the id a
    label is `for`, a radio button's id or value): `in-progress` -> in, progress. Only codes
    written as names (letters and separators, no digit): anything else could be a record's key.
    A choice whose text only repeats these words is the application's code list (`Open` for
    `value="open"`): the snapshot carries the code already, so the text tells nothing more."""
    return name_words(*(c for c in codes if c and CODE_NAME.fullmatch(c)))


def name_words(*names: str) -> set[str]:
    """The words of ids and classes: `savedNumber` -> saved, number; `nav__item` -> nav, item."""
    out: set[str] = set()
    for name in names:
        spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name or "")
        out.update(w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w)
    return out


KEEP_ATTRS = {
    # Twin Lab's own markers in a snapshot: where a frame, a shadow root, a canvas or a document is
    # (structure, never data; the text OCR read from a canvas is element text and masked as such).
    "data-tl-frame", "data-tl-mode", "data-tl-canvas", "data-tl-document", "data-tl-for",
    "data-tl-ocr", "data-tl-pending",
    "id", "class", "name", "type", "for", "role", "placeholder", "title", "aria-label", "lang",
    "aria-labelledby", "aria-describedby", "aria-hidden", "aria-expanded", "aria-selected",
    "disabled", "readonly", "required", "checked", "selected", "multiple", "hidden", "rows",
    "cols", "size", "maxlength", "minlength", "min", "max", "step", "pattern", "autocomplete",
    "method", "enctype", "colspan", "rowspan", "scope", "tabindex", "charset", "rel", "media",
    "width", "height", "style", "dir", "novalidate", "ng-version", "data-reactroot", "data-v-app",
    "aria-live", "aria-modal", "aria-controls", "aria-current", "aria-invalid", "aria-required",
    "http-equiv", "viewport", "crossorigin", "inputmode", "spellcheck", "accept", "list",
    # Attributes automations select by (record numbers in them are masked: mask_attributes), and
    # structure that is not data.
    "data-testid", "data-test", "data-test-id", "data-qa", "data-cy", "data-automation-id",
    "target", "autofocus", "accesskey", "align", "valign", "border", "cellpadding", "cellspacing",
    "frameborder", "loading", "sandbox", "allow", "wrap", "draggable", "contenteditable",
    "aria-haspopup", "aria-pressed", "aria-checked", "aria-disabled", "aria-level", "aria-sort",
    "aria-orientation", "aria-multiselectable", "aria-readonly", "aria-busy", "aria-atomic",
    "sizes",  # an icon's or a picture's sizes (`32x32`): the interface's, not a record's
}  # fmt: skip
URL_ATTRS = {"href", "src", "action", "formaction", "poster", "data"}

# What `_Page` notes about the technology a page uses: names of files, attributes, tags, fields.
FACT_KINDS = ("script", "link", "attr", "tag", "name", "meta", "url", "flag", "id")
ATTR_FACT = re.compile(
    r"^(ng-app|ng-controller|ng-model|ng-repeat|ng-click|ng-bind|ng-if|data-ng-app|"
    r"data-ng-controller|ng-version|_ngcontent|_nghost|ng-reflect|data-bs-toggle|data-bs-target|"
    r"data-toggle|data-target|data-bind|data-turbo|data-turbolinks|data-controller|data-wicket|"
    r"data-role|v-cloak|v-if|v-for|data-v-app)"
)
PRODUCT = re.compile(r"^[A-Za-z][A-Za-z0-9.!+-]*(?: \d+(?:\.\d+){0,3})?")
FIELD_SKIP_TYPES = {"hidden", "submit", "button", "reset", "image", "file", "password"}


def _asset_path(value: str) -> str:
    """The path of a script or style address, without host, query or record numbers."""
    return mask_identifiers(urlsplit(value).path)[-160:]


CELL_INT = re.compile(r"^[-+]?(\d{1,3}(,\d{3})+|[1-9]\d*|0)$")
CELL_DEC = re.compile(
    r"^[-+(]?[$€£¥]?\s?\d[\d,]*[.,]\d{1,6}\)?\s?%?$|^[-+]?[$€£¥]\s?\d[\d,]*\)?$|^\d+%$"
)
CELL_DATE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}|\d{1,2}[/.]\d{1,2}[/.]\d{2,4}|\d{1,2}[- ][A-Za-z]{3,9}[- ,]+\d{2,4}|"
    r"[A-Za-z]{3,9}\.? \d{1,2},? \d{4})$"
)
CELL_TIME = r"\d{1,2}:\d{2}(:\d{2})?(\.\d+)?\s?([AaPp][Mm])?\s?(Z|[A-Z]{2,4}|[+-]\d{2}:?\d{2})?"
CELL_DATETIME = re.compile(
    r"^(\d{4}-\d{2}-\d{2}|\d{1,2}[/.]\d{1,2}[/.]\d{2,4}|\d{1,2}[- ][A-Za-z]{3,9}[- ,]+\d{2,4}|"
    r"[A-Za-z]{3,9}\.? \d{1,2},? \d{4})[T ,]+" + CELL_TIME + "$"
)
CELL_BOOL = {"yes", "no", "true", "false", "y", "n", "✓", "✗"}


def cell_shape(text: str) -> str:
    """The shape of one cell's text: integer, decimal, date, datetime, boolean or text."""
    text = text.strip()
    if text.lower() in CELL_BOOL:
        return "boolean"
    if CELL_INT.match(text) and not (len(text) > 1 and text.startswith("0")):
        return "integer"
    if CELL_DEC.match(text):
        return "decimal"
    if CELL_DATETIME.match(text):
        return "datetime"
    if CELL_DATE.match(text):
        return "date"
    return "text"


def cell_type(texts: list[str]) -> str | None:
    """The one type a column's cells share (None when every cell is empty); mixed is text."""
    kinds = {cell_shape(t) for t in texts}
    if not kinds:
        return None
    if len(kinds) == 1:
        return kinds.pop()
    if kinds <= {"integer", "decimal"}:
        return "decimal"
    if kinds <= {"date", "datetime"}:
        return "datetime"
    if kinds <= {"boolean", "text"} and "text" in kinds:
        return "text"
    return "text"


class _Page(HTMLParser):
    """Rebuilds a page without values, and collects what the model needs from it."""

    def __init__(self, known: set[str] = frozenset(), vocab: set[str] = frozenset(),
                 vary: dict[str, set[str]] | None = None, mode: str | None = None):  # fmt: skip
        super().__init__(convert_charrefs=True)
        self.known = known  # words the recording shows as data elsewhere (lower-case)
        # of those, the short values shown whole (`phrases_in`: with a space or a digit)
        self.phrases = {k for k in known if " " in k or any(c.isdigit() for c in k)}
        self.vocab = vocab  # words the interface itself uses (lower-case)
        self.widget_now = False  # the element being opened is a media player's control
        self.vary = vary or {}  # place on the screen -> tokens that differ between snapshots
        self.strict = (mode or page_text_mode()) == "strict"
        from twinlab2_contract.evidence import publicdata

        # public catalogue data the owner declared (off unless approved; never in strict mode)
        self.public = publicdata.current() if not self.strict else None
        self.out: list[str] = []
        self.stack: list[str] = []
        self.with_id: list[bool] = []  # per open element: has an id (then its text is a value)
        self.id_open: list[bool] = []  # per open element: has an id at all (`h3#totalp`)
        # per open element: {"data", "message", "nav", "holder", "key"}; see _context
        self.flags: list[dict] = []
        self.counts: list[dict[str, int]] = [{}]  # per open element: children seen, by tag
        self.texts: list[tuple[str, str]] = []  # (place, text) of interface text: in memory only
        self.text_n: dict[str, int] = {}
        self.vocab_seen: set[str] = set()  # words in labels, buttons, headers, options, menus
        self.messages: list[dict] = []  # message elements and their (masked) text
        self.frames: list[dict] = []  # iframes: selector and address path
        self.drop = 0  # inside a script or template
        self.ids: set[str] = set()
        self.classes: set[str] = set()
        self.elements: list[dict] = []
        self.labels: dict[str, str] = {}  # for= id -> label text
        self.open_label: list[dict] = []  # label being read: {"for", "text", "wraps"}
        self.open_text: list[dict] = []  # element whose kept text is being read
        self.select: dict | None = None
        self.option: list[str] | None = None
        self.codes: dict[str, set[str]] = {}  # open option or label -> its code_words()
        self.title_words: set[str] = set()  # words the (masked) <title> of this page kept
        self.markers: dict[str, str] = {}
        self.title = ""
        self.body_elements = 0
        self.in_body = False
        self.values: set[str] = set()  # words of every text and value masked
        self.names: set[str] = set()  # of those, the words written with a capital (names)
        # Structural facts for technology detection: file names, attribute and tag names, field
        # names, meta names. Never a value (see `_fact`).
        self.facts: dict[str, set[str]] = {k: set() for k in FACT_KINDS}
        self.headings: list[str] = []  # h1/h2 text, masked as any heading
        self.fields: list[dict] = []  # form fields: the element dict and whether it is in a table
        self.tables: list[dict] = []  # open tables being read
        self.grids: list[dict] = []  # data grids: header labels and the type of each column
        self.form_depth = 0

    # attributes ------------------------------------------------------------------------------
    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        kind = (dict(attrs).get("type") or "").lower()
        parts = []
        for name, value in attrs:
            name = name.lower()
            if name.startswith("on") or name in ("srcdoc", "nonce", "integrity"):
                continue
            if value is None:
                parts.append(name)
                continue
            if name == "value":
                keep = tag == "input" and kind in BUTTON_TYPES
                code = tag == "option" or (tag == "input" and kind in ("checkbox", "radio"))
                if self.strict or not (keep or code):
                    self.values |= words(value)
                    self.names |= names_in(value)
                    value = mask(value)
                elif code:  # the code the app posts for a choice: its own, unless it is data
                    value = interface_text(value, self.known, self.vocab, names=False)
            elif name in URL_ATTRS:
                value = _safe_url(value, self.public)
            elif name == "content" and tag == "meta":
                meta = dict(attrs)
                kind_ = (meta.get("name") or meta.get("http-equiv") or "").lower()
                # Only harmless metadata stays: tokens (csrf), user names and the like go.
                value = value if kind_ in ("viewport", "generator", "content-type") else ""
            elif name in TEXT_ATTRS:
                value = self._attr_text(value, tag)
            elif name == "style":
                value = re.sub(r"url\([^)]*\)", "url(#)", value)  # no recorded addresses or tokens
            elif (BEHAVIOUR_ATTR.fullmatch(name) and not self.strict
                  and (ELEMENT_REF.fullmatch(value) or SETTING.fullmatch(value))
                  and value.lower().lstrip("#.") not in self.known
                  and not _has_name_word(value.lower().lstrip("#."), self.known)):  # fmt: skip
                pass  # a reference to an element of the page, or the library's own setting
            elif (name.startswith("data-") and not self.strict and DATA_NAME.fullmatch(value)
                  and value not in self.known and not _has_name_word(value, self.known)):  # fmt: skip
                pass  # a name the page's code uses (`data-field="amount"`), not a value
            elif name not in KEEP_ATTRS:
                self.values |= words(value)
                self.names |= names_in(value)
                value = mask(value)
            parts.append(f'{name}="{escape(value, quote=True)}"')
        return (" " + " ".join(parts)) if parts else ""

    # tags ------------------------------------------------------------------------------------
    def handle_decl(self, decl: str) -> None:
        if decl.lower().startswith("doctype"):
            self.out.append(f"<!{decl}>")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, closed=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, closed=True)

    # technology facts -----------------------------------------------------------------------
    def _fact(self, kind: str, value: str) -> None:
        value = value.strip()
        bucket = self.facts[kind]
        if value and len(value) <= 160 and len(bucket) < 300:
            bucket.add(value)

    def _facts(self, tag: str, a: dict[str, str], raw: dict[str, str]) -> None:
        """Facts about the technology a page is built with. `a` is masked, `raw` is not; only
        names and file names are taken from either, never an attribute value as such."""
        if "-" in tag or ":" in tag:
            self._fact("tag", tag)
        for name in a:
            m = ATTR_FACT.match(name)
            if m:
                self._fact("attr", m.group(1))
        if tag in ("input", "select", "textarea", "form") and a.get("name"):
            self._fact("name", a["name"])
        if tag == "link" and a.get("href") and "icon" not in a.get("rel", ""):
            self._fact("link", _asset_path(a["href"]))
        if tag == "meta":
            meta = a.get("name", "").lower()
            if meta in (
                "csrf-param",
                "csrf-token",
                "generator",
                "_csrf",
                "_csrf_header",
                "viewport",
            ):
                self._fact("meta", meta)
            if meta == "generator":
                m = PRODUCT.match(raw.get("content", "").strip())
                if m:
                    self._fact("meta", f"generator:{m.group(0)}")
        if tag in ("a", "form", "iframe", "frame") and (
            a.get("href") or a.get("action") or a.get("src")
        ):
            value = raw.get("href") or raw.get("action") or raw.get("src") or ""
            if ";jsessionid" in value.lower():
                self._fact("flag", "jsessionid-in-url")
            path, _, query = value.partition("?")
            path = mask_identifiers(urlsplit(path).path.split(";", 1)[0])
            names = ",".join(q.split("=", 1)[0] for q in query.split("#")[0].split("&") if q)[:60]
            self._fact("url", f"{path}?{names}" if names else path)

    def _start(self, tag: str, attrs, closed: bool) -> None:
        raw_attrs = {k.lower(): (v or "") for k, v in attrs}
        attrs = mask_attributes(attrs)  # record numbers in ids and links never leave the evidence
        if tag == "script" and not self.drop:
            src = _asset_path(dict(attrs).get("src") or "")
            if src:
                self._fact("script", src)
            if raw_attrs.get("id"):
                self._fact("id", raw_attrs["id"])
            if raw_attrs.get("type", "").lower() == "text/x-handlebars-template":
                self._fact("flag", "handlebars-template")
        if self.drop:
            if tag in DROP_WITH_CONTENT and not closed:
                self.drop += 1
            return
        if tag in DROP_WITH_CONTENT:
            if not closed:
                self.drop = 1
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "body":
            self.in_body = True
        elif self.in_body and tag not in ("link", "noscript", "style"):
            self.body_elements += 1
        if "ng-version" in a:
            self.markers.setdefault("angular", a["ng-version"] or "")
        if "data-reactroot" in a:
            self.markers.setdefault("react", "data-reactroot")
        if "data-v-app" in a or any(k.startswith("data-v-") for k in a):
            self.markers.setdefault("vue", "data-v- attribute")
        if a.get("name") == "__VIEWSTATE" or a.get("id") == "__VIEWSTATE":
            self.markers.setdefault("aspnet-webforms", "__VIEWSTATE")
        if "javax.faces" in a.get("name", "") or "javax.faces" in a.get("id", ""):
            self.markers.setdefault("jsf", "javax.faces.ViewState")
        if a.get("id"):
            self.ids.add(a["id"])
        self.classes.update(c for c in a.get("class", "").split() if c)
        self._facts(tag, a, raw_attrs)
        self._grid_start(tag, a, closed)
        self.widget_now = bool(self.flags and self.flags[-1].get("widget")) or _player(
            a.get("class", ""))  # fmt: skip
        self._element(tag, a)
        if tag in ("iframe", "frame") and a.get("src"):
            sel = f"{tag}#{a['id']}" if a.get("id") else (
                f'{tag}[name="{a["name"]}"]' if a.get("name") else tag)  # fmt: skip
            self.frames.append({"selector": sel, "src": urlsplit(_safe_url(a["src"])).path,
                                "title": self._attr_text(a.get("title") or "") or None})  # fmt: skip
        if tag in ("input", "select", "textarea", "button") and not self.strict:
            for name in ("aria-label", "placeholder"):
                if a.get(name) and interface_text(a[name], self.known, self.vocab) == a[name]:
                    self.vocab_seen |= words(a[name])

        self.out.append(f"<{tag}{self._attrs(tag, attrs)}{' /' if closed else ''}>")
        self.widget_now = False
        if tag not in VOID and not closed:
            row_header = tag == "th" and (a.get("scope") == "row" or "tbody" in self.stack)
            self.flags.append(self._flags(tag, a, row_header))
            self.stack.append(tag)
            # A row header (or any element with an id) holds a value, not the app's words.
            self.with_id.append(row_header or (bool(a.get("id")) and tag not in KEEP_TEXT))
            self.id_open.append(bool(a.get("id")))
            self.counts.append({})

    def _flags(self, tag: str, a: dict[str, str], row_header: bool) -> dict:
        """What an open element says about the text inside it."""
        parent = self.flags[-1] if self.flags else {"data": False, "message": False, "nav": False,
                                                    "key": ""}  # fmt: skip
        counts = self.counts[-1]
        counts[tag] = counts.get(tag, 0) + 1
        place = f"{tag}#{a['id']}" if a.get("id") else f"{tag}[{counts[tag]}]"
        words_ = name_words(a.get("id", ""), a.get("class", ""))
        role = a.get("role", "").lower()
        message = (role in ("alert", "status", "log") or "aria-live" in a
                   or bool(MESSAGE.search(f"{a.get('id', '')} {a.get('class', '')}"))) and (
            tag not in ("input", "select", "textarea", "table", "form"))  # fmt: skip
        data = (parent["data"] or tag in DATA_TAGS or row_header
                or a.get("contenteditable", "").lower() in ("", "true", "plaintext-only")
                and "contenteditable" in a)  # fmt: skip
        nav = parent["nav"] or tag in ("nav", "menu") or role in (
            "navigation", "menu", "menubar", "tablist") or bool(words_ & NAV_WORDS)  # fmt: skip
        holder = (
            (bool(a.get("id")) or bool(words_ & DATA_WORDS))
            and tag not in KEEP_TEXT
            and (tag not in ("a", "form", "table", "nav", "header", "main", "body", "html"))
            and not message
        )
        classes = ".".join(c for c in a.get("class", "").split()[:2] if c)
        sel = f"{tag}#{a['id']}" if a.get("id") else tag + (f".{classes}" if classes else "") + (
            f'[role="{role}"]' if role and not classes else "")  # fmt: skip
        widget = parent.get("widget", False) or _player(a.get("class", ""))
        # a region the owner declared public (twinlab2_contract.evidence.publicdata): its text is carried as recorded
        public = parent.get("public", False) or bool(self.public and self.public.region(tag, a))
        holder = holder and not widget
        return {"data": data, "message": message or parent["message"], "nav": nav,
                "widget": widget, "public": public,
                "holder": holder, "own_message": message, "key": f"{parent['key']}/{place}",
                "sel": sel, "hidden": "hidden" in a or parent.get("hidden", False), "buf": []}  # fmt: skip

    def _end_message(self) -> None:
        f = self.flags[-1]
        if not f["own_message"] or self.flags[:-1] and self.flags[-2]["message"]:
            return  # a message inside a message is read as part of the outer one
        text = " ".join("".join(f["buf"]).split())
        if text and len(self.messages) < 60:
            self.messages.append({"selector": f["sel"], "text": text[:300], "hidden": f["hidden"]})

    def _context(self) -> str:
        """`data`, `holder` (an element that holds a value), `message` or `interface`."""
        if self.flags and self.flags[-1]["data"]:
            return "data"
        for tag, f in zip(reversed(self.stack), reversed(self.flags), strict=True):
            if tag in KEEP_TEXT or tag == "a":
                return "interface"
            if f["own_message"]:
                return "message"
            if f["holder"]:
                return "holder"
            if tag in PASS_THROUGH or tag in NEUTRAL:
                continue
            return "interface"
        return "interface"

    def _attr_text(self, value: str, tag: str = "") -> str:
        """A tooltip, `aria-label`, placeholder or `alt`. On a control it is that control's
        label, read as one: a word the data also shows (`Open the help pages`) is masked only
        next to another such word or when it is the whole text. Runs of Title-case words
        (names) are masked everywhere."""
        if self.strict:
            return mask(value)
        if self.widget_now:  # a media player's control: its library's own label
            return interface_text(value, self.known, self.vocab, names=False, label=True)
        label = tag in ("input", "select", "textarea", "button")
        return interface_text(value, self.known, self.vocab, label=label)

    def _element(self, tag: str, a: dict[str, str]) -> None:
        kind = a.get("type", "").lower()
        if tag == "label":
            self.open_label.append({"for": a.get("for"), "text": [], "wraps": None})
            self.codes["label"] = code_words(a.get("for") or "")
        elif tag == "input" and kind in ("radio", "checkbox") and "label" in self.codes:
            self.codes["label"] |= code_words(a.get("id") or "", a.get("value") or "")
        el = None
        if a.get("id") or (tag in INTERACTIVE and a.get("name") and kind != "hidden"):
            el = {"tag": tag, "id": a.get("id") or None, "name": a.get("name") or None,
                  "type": kind or None, "classes": a.get("class", "").split(),
                  "label": a.get("aria-label") or a.get("placeholder") or a.get("title") or None,
                  "options": [], "text": []}  # fmt: skip
            if tag == "input" and kind in BUTTON_TYPES and a.get("value"):
                el["label"] = a["value"]
            if el["label"]:  # the same masking as the attribute gets in the page itself
                el["label"] = self._attr_text(el["label"], tag)
            # What a form field accepts, from its own attributes (never what it held).
            if tag in ("input", "select", "textarea"):
                rules = {k: a[k] for k in ("required", "min", "max", "minlength", "maxlength",
                                           "pattern", "step", "inputmode", "multiple", "readonly",
                                           "disabled") if k in a}  # fmt: skip
                for k in ("min", "max", "pattern", "step"):
                    if k in rules:
                        rules[k] = interface_text(rules[k], names=False) if not self.strict else (
                            mask(rules[k]))  # fmt: skip
                if rules:
                    el["rules"] = {k: (v if v != "" else True) for k, v in rules.items()}
            self.elements.append(el)
            if tag in ("input", "select", "textarea") and kind not in FIELD_SKIP_TYPES:
                self.fields.append({"el": el, "frames": list(self.tables)})
            if self.open_label and self.open_label[-1]["wraps"] is None:
                self.open_label[-1]["wraps"] = el
        if tag == "select":
            self.select = el or {"options": []}
        if tag == "option":
            self.option = []
            self.codes["option"] = code_words(a.get("value") or "")
            # The code the option posts: kept only when it reads like a code (letters first, at
            # most two digits); anything else could be data and is masked.
            value = a.get("value")
            if value is not None and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,30}", value) or (
                    value and sum(c.isdigit() for c in value) > 2):  # fmt: skip
                value = mask(value)
            self.option_value = value
        if tag in KEEP_TEXT and tag not in ("label", "option"):
            self.open_text.append({"tag": tag, "el": el, "text": []})

    def handle_endtag(self, tag: str) -> None:
        if self.drop:
            if tag in DROP_WITH_CONTENT:
                self.drop -= 1
            return
        if tag not in self.stack:
            return
        while self.stack:
            top = self.stack.pop()
            self.with_id.pop()
            if self.id_open:
                self.id_open.pop()
            self.out.append(f"</{top}>")
            self._end(top)
            self._end_message()
            self.flags.pop()
            self.counts.pop()
            if top == tag:
                break

    # data grids -----------------------------------------------------------------------------
    def _grid_start(self, tag: str, a: dict[str, str], closed: bool) -> None:
        if tag == "table":
            if self.tables:
                self.tables[-1]["nested"] = True
            self.tables.append({"id": a.get("id") or None, "rows": [], "row": None, "cell": None,
                                "nested": False, "head": False, "inputs": 0,
                                "framed": "tl-frame" in self.stack})  # fmt: skip
            return
        if not self.tables:
            return
        t = self.tables[-1]
        if tag == "thead":
            t["head"] = True
        elif tag == "tbody":
            t["head"] = False
        elif tag == "tr":
            self._grid_close_cell(t)
            self._grid_close_row(t)
            t["row"] = {"cells": [], "head": t["head"], "class": a.get("class", "")}
        elif tag in ("td", "th"):
            self._grid_close_cell(t)
            if t["row"] is None:
                t["row"] = {"cells": [], "head": t["head"], "class": ""}
            span = a.get("colspan", "1")
            t["cell"] = {"tag": tag, "text": [], "span": int(span) if span.isdigit() else 1,
                         "scope": a.get("scope", ""), "controls": 0,
                         "class": a.get("class", "")}  # fmt: skip
        elif tag in ("input", "select", "textarea", "button") and t["cell"] is not None:
            t["cell"]["controls"] += 1

    @staticmethod
    def _grid_close_cell(t: dict) -> None:
        if t["cell"] is not None and t["row"] is not None:
            t["row"]["cells"].append(t["cell"])
        t["cell"] = None

    @staticmethod
    def _grid_close_row(t: dict) -> None:
        if t["row"] is not None and t["row"]["cells"]:
            t["rows"].append(t["row"])
        t["row"] = None

    def _grid_end(self, tag: str) -> None:
        if not self.tables:
            return
        t = self.tables[-1]
        if tag in ("td", "th"):
            self._grid_close_cell(t)
        elif tag == "tr":
            self._grid_close_cell(t)
            self._grid_close_row(t)
        elif tag == "thead":
            t["head"] = False
        elif tag == "table":
            self._grid_close_cell(t)
            self._grid_close_row(t)
            self.tables.pop()
            grid = self._grid(t)
            if grid:
                t["is_grid"] = True
                self.grids.append(grid)

    def _grid(self, t: dict) -> dict | None:
        """A data grid (a header row of th cells over repeated rows of td cells) as column labels
        and the type each column's cells have. The cell texts themselves are dropped here."""
        rows = t["rows"]
        if t["nested"] or len(rows) < 2:
            return None
        head = next(
            (r for r in rows[:2] if r["cells"] and all(c["tag"] == "th" for c in r["cells"])), None
        )
        if head is None or len(head["cells"]) < 2:
            return None
        labels = []
        for c in head["cells"]:
            text = " ".join("".join(c["text"]).split())
            if text and self.strict:
                text = mask(text)
            elif text:
                text = interface_text(text, self.known, self.vocab, names=False, label=True)
            labels.append(text)
        if sum(1 for x in labels if x) < 2:
            return None
        width = len(labels)
        body = [
            r for r in rows[rows.index(head) + 1 :]
            if any(c["tag"] == "td" for c in r["cells"])
            and width - 1 <= len(r["cells"]) <= width + 1
            and all(c["span"] == 1 for c in r["cells"])
        ]  # fmt: skip
        if not body:
            return None
        types: list[str | None] = []
        for i in range(width):
            texts = [" ".join("".join(r["cells"][i]["text"]).split())
                     for r in body if i < len(r["cells"])]  # fmt: skip
            types.append(cell_type([x for x in texts if x]))
        return {"id": t["id"], "headers": labels, "types": types, "rows": len(body),
                "framed": t.get("framed", False), "row_class": body[0].get("class", ""),
                "cell_classes": [c.get("class", "") for c in body[0]["cells"]],
                "optional": [any(i >= len(r["cells"]) or not "".join(r["cells"][i]["text"]).strip()
                                 for r in body) for i in range(width)]}  # fmt: skip

    def _end(self, tag: str) -> None:
        self._grid_end(tag)
        if tag in ("h1", "h2") and self.open_text and self.open_text[-1]["tag"] == tag:
            text = " ".join("".join(self.open_text[-1]["text"]).split())
            if text and len(self.headings) < 6:
                self.headings.append(text)
        if tag in ("label", "option"):
            self.codes.pop(tag, None)
        if tag == "label" and self.open_label:
            label = self.open_label.pop()
            text = " ".join("".join(label["text"]).split())
            if label["for"]:
                self.labels[label["for"]] = text
            elif label["wraps"] is not None and not label["wraps"]["label"]:
                label["wraps"]["label"] = text
        elif tag == "option" and self.option is not None:
            text = " ".join("".join(self.option).split())
            if self.select is not None and text:
                self.select["options"].append(text)
                self.select.setdefault("option_values", []).append(
                    getattr(self, "option_value", None)
                )
            self.option = None
        elif tag == "select":
            self.select = None
        elif self.open_text and self.open_text[-1]["tag"] == tag:
            item = self.open_text.pop()
            text = " ".join("".join(item["text"]).split())
            if tag == "title" and not self.title:
                self.title = text
                # what the title says after masking, the page says already: see handle_data
                self.title_words = {w for w in words(text) if not re.fullmatch(r"x+", w)}
            if item["el"] is not None and not item["el"]["label"]:
                item["el"]["label"] = text or None

    def handle_data(self, data: str) -> None:
        if self.drop:
            return
        if self.tables and self.tables[-1]["cell"] is not None:
            self.tables[-1]["cell"]["text"].append(data)  # read for its shape only, then dropped
        if self.stack and self.stack[-1] == "style":
            css = _style(data)
            # raw text, as HTML keeps a style element's content (`&gt;` there is not `>` to a
            # browser, so an escaped `a>b` rule is dropped); only a closing tag is broken
            css = mask(css) if self.strict else css
            self.out.append(re.sub(r"(?i)</(style)", r"<\\/\1", css))
            return
        ctx = self._context()
        if self._public_text(data, ctx):
            text = data
        elif ctx == "data" and not self.strict and self._row_control(data):
            text = data  # a row's own control (`Delete` in each cart row): the interface's word
        elif ctx == "data" or self.strict:
            if ctx == "data" or self._data_cell():
                self.values |= words(data)  # only text where records are shown counts as data
                self.names |= names_in(data)
            text = mask(data)
        else:
            own = next((t for t in reversed(self.stack) if t in KEEP_TEXT), "")
            key = self.flags[-1]["key"] if self.flags else ""
            if data.strip():
                n = self.text_n[key] = self.text_n.get(key, -1) + 1
                key = f"{key}~{n}"
                self.texts.append((key, data))
            in_vocab = (own in VOCAB_TAGS and not (own == "th" and "tbody" in self.stack)) or (
                self.flags and self.flags[-1]["nav"] and own in ("", "a", "button"))  # fmt: skip
            # the place's tokens that change between snapshots; not for labels, buttons, headers,
            # options and messages: those change with what the app says, not with the data
            vary = set() if ctx == "message" or own in VOCAB_TAGS else self.vary.get(key, set())
            label = own in LABEL_TAGS or own in ("option", "optgroup")  # code lists are labels too
            vocab = self.vocab
            code = self.codes.get(own) if own in ("option", "label") else None
            if code and words(data) and words(data) <= code | vocab:
                vocab = vocab | code  # the choice's text repeats its own code: see code_words()
            elif ctx == "holder" and self.title_words:
                vocab = vocab | self.title_words  # the application's name, as the title has it
            widget = bool(self.flags and self.flags[-1].get("widget"))
            if widget:  # a media player's own controls: read as labels (see PLAYER_CLASS)
                text = interface_text(data, self.known, vocab, names=False, label=True)
            elif self._phrase(data, vocab):  # a record's value shown whole (a title, a link)
                self.values |= words(data)
                text = mask(data)
            else:
                text = interface_text(data, self.known, vocab, vary, names=own not in LABEL_TAGS,
                                      holder=ctx == "holder", label=label)  # fmt: skip
            # the interface's own words: what such a place says, unless it reads like a name
            if (
                in_vocab
                and text == data
                and interface_text(data, self.known, self.vocab, label=label) == data
            ):
                self.vocab_seen |= words(data)
        for label in self.open_label:
            label["text"].append(text)
        for item in self.open_text:
            item["text"].append(text)
        if self.option is not None:
            self.option.append(text)
        for f in self.flags:
            if f["own_message"]:
                f["buf"].append(text)
        self.out.append(escape(text, quote=False))

    def _public_text(self, data: str, ctx: str) -> bool:
        """A text the owner declared public: in a declared region, or, outside data cells (a
        cart's rows), a declared value as a whole (`publicdata.Public.text`). The secret checks
        apply to both."""
        from twinlab2_contract.evidence import publicdata

        if self.public is None or not data.strip() or publicdata.refused("", data.strip()):
            return False
        if self.flags and self.flags[-1].get("public"):
            return True
        # a table or description cell is a record's row (a cart's lines stay masked); an element
        # with an id (`p#article` of a product card) shows a declared value as the API answered it
        in_cell = any(t in ("td", "dd") for t in self.stack)
        if self.id_open and self.id_open[-1] and re.fullmatch(r"[\s$€£¥\d.,]+", data):
            return False  # a number in an element of its own (`h3#totalp`): a sum, not a price

        return ctx != "data" and not in_cell and self.public.text(data)

    def _row_control(self, data: str) -> bool:
        """The text of a link or button inside a data cell that is one plain word (`Delete`,
        `Edit`, `Open`) no recorded answer or page shows as data: the control's own label."""
        text = data.strip()
        return (bool(self.stack) and self.stack[-1] in ("a", "button")
                and bool(re.fullmatch(r"[A-Za-z][a-z]{1,19}", text))
                and text.lower() not in self.known)  # fmt: skip

    def _phrase(self, data: str, vocab: set[str]) -> bool:
        """Whether a text is, as a whole, a short value the recording's answers hold
        (`phrases_in`), and not only words of the interface itself."""
        p = phrase(data)
        return bool(p) and p in self.phrases and not words(p) <= vocab

    def _data_cell(self) -> bool:
        """Inside a table or description cell, or an element with an id: where records show."""
        return any(t in ("td", "dd") for t in self.stack) or bool(self.with_id and self.with_id[-1])

    def handle_comment(self, data: str) -> None:
        return

    def close(self) -> None:
        super().close()
        while self.stack:
            top = self.stack.pop()
            self.with_id.pop()
            if self.id_open:
                self.id_open.pop()
            self.out.append(f"</{top}>")
            self._end(top)
            self._end_message()
            self.flags.pop()
            self.counts.pop()
        for el in self.elements:
            if el["id"] and el["id"] in self.labels and not el["label"]:
                el["label"] = self.labels[el["id"]] or None
            el.pop("text", None)
        self.fields = [
            {**f["el"], "in_grid": any(t.get("is_grid") for t in f["frames"])} for f in self.fields
        ]


def _style(css: str) -> str:
    """A style sheet as the application wrote it, without addresses (`url(...)`) and with the
    text of `content:` strings masked (the one place CSS can carry words; a string without a
    word, such as an icon font's glyph, stays)."""
    css = re.sub(r"url\([^)]*\)", "url(#)", css)
    css = re.sub(r"@import\s+[^;]+;", "", css)
    return re.sub(
        r"(content\s*:\s*)(\"[^\"]*\"|'[^']*')",
        lambda m: m.group(1) + (m.group(2) if css_symbol(m.group(2)) else mask(m.group(2))),
        css,
        flags=re.IGNORECASE,
    )


CSS_ESCAPE = re.compile(r"\\[0-9a-fA-F]{1,6} ?|\\.")


def css_symbol(string: str) -> bool:
    """Whether a CSS string holds no word: only escapes (an icon font's glyph, a backslash and
    its code point), punctuation, symbols or single letters. Such a string is kept as it is."""
    return not WORD.search(CSS_ESCAPE.sub(" ", string)) and not re.search(
        r"\d{3}", CSS_ESCAPE.sub(" ", string)
    )


def _safe_url(value: str, public=None) -> str:
    """An address without values: the path as a pattern, each query parameter by name. A value
    the owner declared public (a product's id of a declared answer: `prod.html?idp_=1`) stays."""
    parts = urlsplit(value)
    if parts.scheme in ("javascript", "data"):
        return "#"
    if parts.scheme in ("mailto", "tel", "sms"):
        return f"{parts.scheme}:{mask(value.split(':', 1)[1])}"
    path = route_pattern(parts.path) if parts.path else ""

    def param(q: str) -> str:
        key, _, val = q.partition("=")
        keep = public is not None and bool(val) and public.text(val)
        return f"{key}={val if keep else ''}"

    query = "&".join(param(q) for q in parts.query.split("&") if q)
    fragment = (
        f"#{parts.fragment}" if parts.fragment and not re.search(r"\d", parts.fragment) else ""
    )
    return path + (f"?{query}" if query else "") + fragment


class PageInfo:
    """A value-free page: its HTML, ids, classes, elements and framework markers."""

    def __init__(self, parser: _Page):
        self.html = "".join(parser.out)
        self.ids = parser.ids
        self.classes = parser.classes
        self.elements = parser.elements
        self.markers = parser.markers
        self.facts = parser.facts
        self.headings = parser.headings
        self.fields = parser.fields
        self.grids = parser.grids
        self.title = parser.title
        self.values = parser.values
        self.names = parser.names
        self.body_elements = parser.body_elements
        self.vocab = parser.vocab_seen
        self.texts = parser.texts  # (place, raw text): in memory only, to find what varies
        self.messages = parser.messages
        self.frames = parser.frames
        self.mode = "strict" if parser.strict else "interface"

    @property
    def has_content(self) -> bool:
        """False for an empty single-page-app shell (`<div id="root"></div>` and scripts)."""
        return self.body_elements > 3 or any(
            e["tag"] in ("input", "select", "textarea", "form") for e in self.elements
        )


def page(
    html: str,
    known: set[str] = frozenset(),
    vocab: set[str] | None = None,
    vary: dict[str, set[str]] | None = None,
    mode: str | None = None,
) -> PageInfo:
    """Parse a recorded page. The result holds no recorded value.

    `known` is every word the same recording shows as data elsewhere; `vocab` every word its
    interface uses (labels, buttons, headers, options, menus); `vary` the tokens that differ
    between snapshots of the same screen, by place (`varying()`). `mode` is `interface` or
    `strict` (default: `page_text_mode()`).
    """
    first = _Page(known, vocab or set(), vary, mode)  # first pass: what this page shows as data
    first.feed(html)
    first.close()
    words_ = set(vocab or set())
    parser = _Page((set(known) | first.names) - words_, words_, vary, mode)
    parser.feed(html)
    parser.close()
    return PageInfo(parser)


def varying(pages: list[PageInfo]) -> dict[str, set[str]]:
    """Per place on a screen, the tokens that are not in every snapshot's text there: data that
    changes from one record to the next. `pages` are snapshots of one screen."""
    by_place: dict[str, list[str]] = {}
    for info in pages:
        seen: dict[str, str] = {}
        for key, text in info.texts:
            seen[key] = seen.get(key, "") + " " + text
        for key, text in seen.items():
            by_place.setdefault(key, []).append(" ".join(text.split()))
    out: dict[str, set[str]] = {}
    for key, texts in by_place.items():
        variants = {t for t in texts if t}
        if len(variants) < 2:
            continue
        tokens = [interface_tokens(t) for t in variants]
        common = set.intersection(*tokens)
        changed = set().union(*tokens) - common
        if changed:
            out[key] = changed
    return out
