# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Reading a DOM snapshot (outerHTML) into the element states the catalog and fidelity compare.

Snapshots written by the harness bot carry the live state as attributes (`value` on inputs,
`selected` on options, `checked` on boxes), set just before `outerHTML` is read. Snapshots
without them still parse; values are then unknown (None), not empty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
    "track", "wbr",
}  # fmt: skip
SKIP_TEXT = {"script", "style", "template", "noscript"}
INTERACTIVE = {"input", "select", "textarea", "button"}
FORM_TAGS = {"input", "select", "textarea"}
TEXT_CAP = 200
# Classes a widget library sets while the pointer is over an element or it has the keyboard focus
# (PrimeFaces, jQuery UI): where the mouse happens to be, not the page's state; and the mark of a
# field that holds a value, which PrimeFaces sets when the field is left (the value itself is
# compared). Not compared.
POINTER_CLASSES = {"ui-state-hover", "ui-state-focus", "ui-state-filled"}

# Run as an init script before the page's own code: remembers every shadow root, closed ones
# included (page code cannot reach a closed root afterwards, and neither could a recorder).
SHADOW_HOOK_JS = r"""(() => {
  if (window.__tlShadows) return;
  const roots = new WeakMap();
  const list = [];
  Object.defineProperty(window, "__tlShadows", { value: roots });
  Object.defineProperty(window, "__tlShadowList", { value: list });
  const attach = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function (init) {
    const root = attach.call(this, init);
    roots.set(this, root);
    list.push(root);
    if (window.__tlOnShadow) window.__tlOnShadow(root);  // the recorder listens inside it
    return root;
  };
})();"""

# The page as HTML, with what outerHTML leaves out written in as ordinary elements, so every
# reader of a snapshot (behaviour catalog, page snapshots, fidelity) sees it:
# - a shadow root (open, or closed when SHADOW_HOOK_JS ran) as <tl-shadow data-tl-mode> inside
#   its host;
# - a frame's document as <tl-frame data-tl-frame="iframe#x"> right after the <iframe>; a
#   cross-origin frame cannot be read from here, so it is left as data-tl-pending="fN" for the
#   recorder to fill in (fill_frames);
# - a <canvas> marked data-tl-canvas="cN" (a picture and its text are added by the recorder);
# - an embedded PDF marked data-tl-document="pdf".
# With {values: true} live form values are written into the markup (fidelity, harness); without,
# values stay as the page's own attributes. Password values are never written.
SERIALIZE_FN = r"""(opts) => {
  opts = opts || {};
  const values = !!opts.values;
  const VOID = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                        "meta", "source", "track", "wbr"]);
  const RAW = new Set(["script", "style"]);
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const escA = (s) => String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;");
  function sel(el) {
    const tag = el.localName;
    if (el.id) return tag + "#" + CSS.escape(el.id);
    const name = el.getAttribute("name");
    if (name) return tag + '[name="' + name + '"]';
    const classes = Array.from(el.classList).slice(0, 2).map((c) => "." + CSS.escape(c)).join("");
    return tag + classes;
  }
  function mark(el, attr, prefix, counter) {
    let id = el.getAttribute(attr);
    if (!id) {
      const w = el.ownerDocument.defaultView || window;
      w[counter] = (w[counter] || 0) + 1;
      id = prefix + w[counter];
      el.setAttribute(attr, id);
    }
    return id;
  }
  function isPdf(el) {
    const t = (el.getAttribute("type") || "").toLowerCase();
    const src = (el.getAttribute("src") || el.getAttribute("data") || "").toLowerCase();
    return t === "application/pdf" || /\.pdf($|[?#])/.test(src);
  }
  function attrs(el) {
    let out = "";
    const tag = el.localName;
    for (const a of Array.from(el.attributes)) {
      if (a.name.startsWith("data-tl-")) continue;  // markers set on the live page
      if (tag === "input" && el.type === "password" && a.name === "value") continue;
      if (values && ((a.name === "value" && (tag === "input" || tag === "textarea"))
                     || (a.name === "selected" && tag === "option") || a.name === "checked")) continue;
      out += " " + a.name + (a.value === "" ? "" : '="' + escA(a.value) + '"');
    }
    if (values) {
      if (tag === "input") {
        if (el.type === "checkbox" || el.type === "radio") { if (el.checked) out += " checked"; }
        else if (el.type !== "password") out += ' value="' + escA(el.value) + '"';
      } else if (tag === "textarea") out += ' value="' + escA(el.value) + '"';
      else if (tag === "option" && el.selected) out += " selected";
    }
    if (tag === "canvas") {
      out += ' data-tl-canvas="' + mark(el, "data-tl-canvas-id", "c", "__tlCanvasSeq") + '"';
    }
    if ((tag === "embed" || tag === "object" || tag === "iframe") && isPdf(el)) {
      out += ' data-tl-document="pdf"';
    }
    return out;
  }
  function shadowOf(el) {
    if (el.shadowRoot) return [el.shadowRoot, "open"];
    const w = el.ownerDocument.defaultView;
    const root = w && w.__tlShadows ? w.__tlShadows.get(el) : null;
    return root ? [root, root.mode || "closed"] : null;
  }
  function kids(node) {
    let s = "";
    for (const c of node.childNodes) s += ser(c);
    return s;
  }
  function frame(el) {
    const path = sel(el);
    let doc = null;
    try { doc = el.contentDocument; } catch (e) { doc = null; }
    if (doc && doc.documentElement) {
      return '<tl-frame data-tl-frame="' + escA(path) + '">' + kids(doc.body || doc.documentElement) + "</tl-frame>";
    }
    const id = mark(el, "data-tl-frame-id", "f", "__tlFrameSeq");
    return '<tl-frame data-tl-frame="' + escA(path) + '" data-tl-pending="' + id + '"></tl-frame>';
  }
  function ser(node) {
    if (node.nodeType === 3) {
      const p = node.parentNode;
      return p && p.localName && RAW.has(p.localName) ? node.data : esc(node.data);
    }
    if (node.nodeType !== 1) return "";
    const tag = node.localName;
    let s = "<" + tag + attrs(node) + ">";
    if (VOID.has(tag)) return s;
    const root = shadowOf(node);
    if (root) s += '<tl-shadow data-tl-mode="' + root[1] + '">' + kids(root[0]) + "</tl-shadow>";
    s += tag === "template" && node.content ? kids(node.content) : kids(node);
    s += "</" + tag + ">";
    if ((tag === "iframe" || tag === "frame") && !isPdf(node)) s += frame(node);
    return s;
  }
  if (opts.body) return kids(document.body || document.documentElement);
  return "<!DOCTYPE html>\n" + ser(document.documentElement);
}"""

# The page with live values, for comparing a replay with a recording.
SYNC_JS = "() => (" + SERIALIZE_FN + ")({values: true})"
FRAME_BODY_JS = "(values) => (" + SERIALIZE_FN + ")({values: values, body: true})"
PENDING = re.compile(r'<tl-frame data-tl-frame="([^"]*)" data-tl-pending="(f\d+)"></tl-frame>')


def fill_frames(page, html: str, values: bool = True, rounds: int = 4) -> str:
    """Write the documents of cross-origin frames into a serialized page. Playwright reaches every
    frame; the page's own script cannot. Frames that cannot be read stay marked as pending."""
    for _ in range(rounds):
        pending = dict((fid, path) for path, fid in PENDING.findall(html))
        if not pending:
            return html
        filled = False
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                element = frame.frame_element()
                fid = element.get_attribute("data-tl-frame-id")
            except Exception:  # noqa: BLE001 - a frame that went away while we looked
                continue
            if fid not in pending:
                continue
            try:
                body = frame.evaluate(FRAME_BODY_JS, values)
            except Exception:  # noqa: BLE001
                continue
            marker = f'<tl-frame data-tl-frame="{pending[fid]}" data-tl-pending="{fid}"></tl-frame>'
            html = html.replace(
                marker, f'<tl-frame data-tl-frame="{pending[fid]}">{body}</tl-frame>'
            )
            filled = True
        if not filled:
            break
    return html


def page_html(page, values: bool = True) -> str:
    """The page with its frames and shadow roots, for the harness bot and fidelity replay."""
    return fill_frames(page, page.evaluate(SYNC_JS if values else
                                           "() => (" + SERIALIZE_FN + ")({values: false})"), values)  # fmt: skip


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    parent: Node | None = None
    children: list[Node] = field(default_factory=list)
    text: list[str] = field(default_factory=list)  # direct and descendant text, in order

    @property
    def id(self) -> str | None:
        return self.attrs.get("id") or None

    @property
    def classes(self) -> list[str]:
        return sorted((self.attrs.get("class") or "").split())


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {})
        self.stack: list[Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        from twinlab2_contract.evidence.sanitize import mask_attributes

        attrs = mask_attributes(attrs)  # record numbers in ids never reach behaviours.json
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if any(n.tag in SKIP_TEXT for n in self.stack):
            return
        for node in self.stack[1:]:
            node.text.append(data)


def parse_html(html: str) -> Node:
    builder = _Builder()
    builder.feed(html or "")
    builder.close()
    return builder.root


def _walk(node: Node):
    for child in node.children:
        yield child
        yield from _walk(child)


def _text(node: Node) -> str:
    return re.sub(r"\s+", " ", "".join(node.text)).strip()


def _hidden_self(node: Node) -> bool:
    if "hidden" in node.attrs:
        return True
    style = (node.attrs.get("style") or "").replace(" ", "").lower()
    if "display:none" in style or "visibility:hidden" in style:
        return True
    return node.tag == "input" and (node.attrs.get("type") or "").lower() == "hidden"


def _visible(node: Node) -> bool:
    cur: Node | None = node
    while cur is not None and cur.tag != "#document":
        if _hidden_self(cur):
            return False
        cur = cur.parent
    return True


HIDING_CLASSES = {"hidden", "d-none", "vjs-hidden", "is-hidden", "invisible"}


def is_dialog(node: Node) -> bool:
    """A dialog by its markup: `<dialog>`, role dialog/alertdialog, `aria-modal`, or a
    Bootstrap/Foundation-style `modal` container."""
    role = (node.attrs.get("role") or "").lower()
    classes = set((node.attrs.get("class") or "").split())
    return (node.tag == "dialog" or role in ("dialog", "alertdialog")
            or node.attrs.get("aria-modal") == "true" or "modal" in classes)  # fmt: skip


def closed_by_markup(node: Node) -> bool:
    """Whether an element's own markup says it is not drawn, beyond `hidden` and inline styles:
    a hiding class (`d-none`, `vjs-hidden`), a `<dialog>` without `open`, and a dialog that is
    `aria-hidden="true"` or a `modal` without `show`/`in` and not displayed by its style (how
    Bootstrap keeps every modal in the page, closed)."""
    classes = set((node.attrs.get("class") or "").split())
    if classes & HIDING_CLASSES:
        return True
    if node.tag == "dialog":
        return "open" not in node.attrs
    if not is_dialog(node):
        return False
    if (node.attrs.get("aria-hidden") or "").lower() == "true":
        return True
    style = (node.attrs.get("style") or "").replace(" ", "").lower()
    return "modal" in classes and not classes & {"show", "in"} and "display:block" not in style


def shown(node: Node) -> bool:
    """Visible as the page drew it: `_visible`, and no element on the way up closed by its
    markup (`closed_by_markup`): a closed modal's fields are not on the screen."""
    cur: Node | None = node
    while cur is not None and cur.tag != "#document":
        if _hidden_self(cur) or closed_by_markup(cur):
            return False
        cur = cur.parent
    return True


def _enabled(node: Node) -> bool:
    cur: Node | None = node
    while cur is not None and cur.tag != "#document":
        if "disabled" in cur.attrs and cur.tag in INTERACTIVE | {"fieldset", "option", "optgroup"}:
            return False
        cur = cur.parent
    return True


@dataclass
class ElementState:
    key: str  # "#id", or "tag[name='x']"
    tag: str
    id: str | None
    classes: list[str]
    visible: bool
    enabled: bool
    interactive: bool
    value: str | None = None  # for form elements; select: the selected option's value
    selected: str | None = None  # select: the selected option's text
    options: list[str] | None = None  # select: option texts in order
    text: str | None = None  # non-form elements: their text content (capped)
    container: bool = False  # holds elements with an id or interactive elements
    rows: int | None = None  # tables and lists: how many rows (body rows, items) they show

    def signature(self) -> str:
        return (
            self.tag + (f"#{self.id}" if self.id else "") + "".join(f".{c}" for c in self.classes)
        )


@dataclass
class Snapshot:
    elements: dict[str, ElementState]  # keyed elements
    signatures: list[str]  # every interactive element, keyed or not, as tag#id.classes


def _is_interactive(node: Node) -> bool:
    if node.tag in INTERACTIVE:
        return not (node.tag == "input" and (node.attrs.get("type") or "").lower() == "hidden")
    return node.tag == "a" and "href" in node.attrs


def _state(node: Node, key: str) -> ElementState:
    st = ElementState(
        key=key,
        tag=node.tag,
        id=node.id,
        classes=[c for c in node.classes if c not in POINTER_CLASSES],
        visible=_visible(node),
        enabled=_enabled(node),
        interactive=_is_interactive(node),
    )
    if node.tag == "select":
        opts = [d for d in _walk(node) if d.tag == "option"]
        st.options = [_text(o) for o in opts]
        chosen = [o for o in opts if "selected" in o.attrs]
        if chosen:
            st.value = chosen[-1].attrs.get("value", _text(chosen[-1]))
            st.selected = _text(chosen[-1])
    elif node.tag == "input":
        kind = (node.attrs.get("type") or "text").lower()
        if kind in ("checkbox", "radio"):
            st.value = "checked" if "checked" in node.attrs else "unchecked"
        elif "value" in node.attrs:
            st.value = node.attrs["value"]
    elif node.tag == "textarea":
        st.value = node.attrs["value"] if "value" in node.attrs else _text(node) or None
    elif node.tag != "button":
        st.text = _text(node)[:TEXT_CAP]
    if node.tag in ("button", "a"):
        st.text = _text(node)[:TEXT_CAP]
    if node.tag not in FORM_TAGS:
        st.container = any(d.id or _is_interactive(d) for d in _walk(node))
    if node.tag in ("table", "tbody"):
        st.rows = sum(1 for d in _walk(node) if d.tag == "tr"
                      and not any(a.tag == "thead" for a in _ancestors(d, node)))  # fmt: skip
    elif node.tag in ("ul", "ol"):
        st.rows = sum(1 for d in node.children if d.tag == "li")
    return st


def _ancestors(node: Node, stop: Node):
    cur = node.parent
    while cur is not None and cur is not stop:
        yield cur
        cur = cur.parent


def snapshot(html: str) -> Snapshot:
    root = parse_html(html)
    elements: dict[str, ElementState] = {}
    signatures: list[str] = []
    for node in _walk(root):
        key = None
        if node.id:
            key = f"#{node.id}"
        elif node.attrs.get("name") and node.tag in INTERACTIVE:
            key = f"{node.tag}[name='{node.attrs['name']}']"
        interactive = _is_interactive(node)
        if key and key not in elements:
            elements[key] = _state(node, key)
        if interactive:
            sig = node.tag + (f"#{node.id}" if node.id else "")
            sig += "".join(f".{c}" for c in node.classes if c not in POINTER_CLASSES)
            signatures.append(sig)
    return Snapshot(elements=elements, signatures=signatures)


def css_unescape(ident: str) -> str:
    r"""An identifier as written in a selector (`CSS.escape`), back to the id itself:
    `loginForm\:username` -> `loginForm:username`, `\31 0th` -> `10th`."""

    def one(m: re.Match) -> str:
        return chr(int(m.group(1), 16)) if m.group(1) else m.group(2)

    return re.sub(r"\\(?:([0-9a-fA-F]{1,6}) ?|(.))", one, ident)


def key_of(selector: str | None) -> str | None:
    r"""The element key a CSS selector names: its last #id, or tag[name=...]. An id is the
    element's own, not as the selector escapes it: JSF ids hold colons (`form:field`), written
    `#form\:field` in a selector, and the snapshot's key is `#form:field`."""
    if not selector:
        return None
    ids = re.findall(r"#((?:\\[0-9a-fA-F]{1,6} ?|\\.|[\w-])+)", selector)
    if ids:
        return f"#{css_unescape(ids[-1])}"
    m = re.search(r"(\w+)\[name=['\"]?([\w-]+)['\"]?\]", selector)
    if m:
        return f"{m.group(1)}[name='{m.group(2)}']"
    return None


def shape(value: str | None) -> str | None:
    """A value's format with the data taken out: letters -> A, digits -> 9, runs collapsed."""
    if value is None:
        return None
    out = []
    for ch in value:
        c = "A" if ch.isalpha() else "9" if ch.isdigit() else ch
        if not out or out[-1] != c or c not in "A9":
            out.append(c)
    return "".join(out)
