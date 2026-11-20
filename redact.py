"""Filtering for the builder package beyond typed values.

The recorder already drops typed values; what reaches the catalog can still carry sensitive
data in other shapes. Everything that goes into a builder package passes through here:

- **text** (flow labels, screens, notes, scenario text, text attachments): e-mail addresses
  (except reserved example domains), US SSNs, payment card numbers (Luhn-checked), IBANs
  (mod-97-checked), international phone numbers, JWTs, bearer tokens, well-known API key
  formats, `password=…`-style assignments and URL query values are replaced by
  `[REDACTED:<kind>]`;
- **JSON** (attachments, HAR bodies): fields whose name says what they hold (password, token,
  cookie, email, ssn, card, iban, phone, address, birth date, …) are replaced whole; every other
  string goes through the text rules;
- **HAR** (recorded HTTP traffic): request and response headers that carry credentials
  (Cookie, Set-Cookie, Authorization, API-key headers) and cookie lists are cleared, query and
  form values are redacted, JSON bodies go through the JSON rules, text bodies through the text
  rules, and binary or base64 bodies are dropped;
- **images** (screenshots): never included by default. When the owner marks one reviewed or
  synthetic, its metadata (PNG text/EXIF/time chunks, JPEG APP1–APP15 and comments) is removed;
  an image that does not parse is excluded.

Every function returns what it found as a `Counter` of kinds, so the pre-freeze report can say
what was redacted and why.
"""

from __future__ import annotations

from collections import Counter
import json
import re
import struct
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MARK = "[REDACTED:{}]"
EXAMPLE_DOMAINS = re.compile(r"(^|\.)(example\.(com|org|net)|example|test|invalid|localhost)$", re.I)

SENSITIVE_FIELD = re.compile(
    r"^(.*pass(word|wd|phrase)?.*|pwd|.*secret.*|.*token.*|.*auth.*|.*cookie.*|.*session.*|.*api_?key.*|"
    r".*access_?key.*|.*private_?key.*|ssn|.*social_?security.*|.*card_?(number|no)?|.*cvv.*|cvc|pan|.*iban.*|"
    r".*account_?(number|no)|.*e_?mail.*|.*phone.*|mobile|.*address.*|dob|.*birth.*|.*tax_?id.*|.*salary.*|"
    r".*national_?id.*|.*passport.*|pin|.*otp.*|.*signature.*)$"
)
SENSITIVE_HEADERS = {"cookie", "set-cookie", "authorization", "proxy-authorization", "x-api-key", "api-key",
                     "x-auth-token", "x-csrf-token", "x-xsrf-token", "x-amz-security-token"}

_EMAIL = re.compile(r"\b[\w.+%-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b")
_PHONE = re.compile(r"(?<![\w+])\+\d{1,3}[ .-]?\(?\d{1,4}\)?(?:[ .-]?\d{2,4}){2,4}\b")
_JWT = re.compile(r"\beyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{5,}")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[\w.~+/-]{8,}=*")
_KEYS = re.compile(r"\b(AKIA[0-9A-Z]{16}|(?:sk|pk|rk)_(?:live|test)_[\w]{10,}|sk-[A-Za-z0-9_-]{20,}"
                   r"|gh[pousr]_[A-Za-z0-9]{30,}|xox[abprs]-[\w-]{10,}|AIza[\w-]{35})\b")
_ASSIGN = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)"
                     r"(\s*[\"']?\s*[:=]\s*[\"']?)([^\s\"',;&<>]+)")
_URL = re.compile(r"\bhttps?://[^\s\"'<>]+", re.I)


def _luhn(digits: str) -> bool:
    total, odd = 0, False
    for ch in reversed(digits):
        n = int(ch)
        if odd:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
        odd = not odd
    return total % 10 == 0


def _iban_ok(text: str) -> bool:
    value = text.replace(" ", "")
    if not 15 <= len(value) <= 34:
        return False
    moved = value[4:] + value[:4]
    try:
        return int("".join(str(int(ch, 36)) for ch in moved)) % 97 == 1
    except ValueError:
        return False


def redact_url(url: str, found: Counter) -> str:
    """A URL without credentials and with its query values redacted."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
    except ValueError:
        found["url"] += 1
        return MARK.format("url")
    if parts.username or parts.password:
        found["url credentials"] += 1
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        query = urlencode([(k, MARK.format("query")) for k, _ in pairs], safe="[]:")
        found["query value"] += len(pairs) or 1
    if parts.fragment:
        found["url fragment"] += 1
    return urlunsplit((parts.scheme, host, parts.path, query, ""))


def redact_text(text: str, found: Counter | None = None) -> str:
    """`text` with the patterns of this module replaced; counts go to `found`."""
    found = Counter() if found is None else found
    if not isinstance(text, str) or not text:
        return text

    def sub(pattern, kind, check=None, group=0):
        nonlocal text

        def replace(match):
            if check is not None and not check(match):
                return match.group(0)
            found[kind] += 1
            if group:
                return match.group(0)[: match.start(group) - match.start(0)] + MARK.format(kind)
            return MARK.format(kind)

        text = pattern.sub(replace, text)

    text = _URL.sub(lambda m: redact_url(m.group(0), found) if ("?" in m.group(0) or "@" in m.group(0)
                                                                 or "#" in m.group(0)) else m.group(0), text)
    sub(_JWT, "token")
    sub(_BEARER, "token")
    sub(_KEYS, "api key")
    sub(_ASSIGN, "secret", group=3)
    sub(_EMAIL, "email", check=lambda m: not EXAMPLE_DOMAINS.search(m.group(1)))
    sub(_SSN, "ssn")
    sub(_IBAN, "iban", check=lambda m: _iban_ok(m.group(0)))
    sub(_CARD, "card number", check=lambda m: _luhn(re.sub(r"\D", "", m.group(0))))
    sub(_PHONE, "phone")
    return text


def sensitive_field(name) -> bool:
    key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name)).lower()
    key = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
    return bool(SENSITIVE_FIELD.match(key))


def redact_json(value, found: Counter | None = None):
    """A copy of a JSON value with sensitive fields replaced and strings redacted."""
    found = Counter() if found is None else found
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if sensitive_field(key) and item not in (None, "", [], {}):
                found[f"field {str(key).lower()}"] += 1
                out[key] = MARK.format("field")
            else:
                out[key] = redact_json(item, found)
        return out
    if isinstance(value, list):
        return [redact_json(item, found) for item in value]
    if isinstance(value, str):
        return redact_text(value, found)
    return value


def _dump(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _body(text, mime, found):
    if not isinstance(text, str) or not text:
        return text
    if "json" in (mime or "").lower() or text.lstrip()[:1] in ("{", "["):
        try:
            return json.dumps(redact_json(json.loads(text), found), ensure_ascii=False, sort_keys=True)
        except ValueError:
            pass
    if (mime or "").lower().startswith("application/x-www-form-urlencoded"):
        pairs = parse_qsl(text, keep_blank_values=True)
        found["form value"] += len(pairs)
        return urlencode([(k, MARK.format("form")) for k, _ in pairs], safe="[]:")
    return redact_text(text, found)


def _headers(headers, found):
    out = []
    for header in headers or []:
        if not isinstance(header, dict):
            continue
        name = str(header.get("name", ""))
        if name.lower() in SENSITIVE_HEADERS or sensitive_field(name):
            found[f"header {name.lower()}"] += 1
            out.append({"name": name, "value": MARK.format("header")})
        else:
            out.append({"name": name, "value": redact_text(str(header.get("value", "")), found)})
    return out


TEXT_MIME = re.compile(r"^(text/|application/(json|xml|javascript|x-www-form-urlencoded)|.*\+(json|xml))", re.I)


def redact_har(data: bytes, found: Counter | None = None) -> bytes:
    """A HAR file with credentials, query/form values and sensitive body fields removed.
    ValueError when it is not a HAR."""
    found = Counter() if found is None else found
    doc = json.loads(data.decode("utf-8-sig"))
    log = doc.get("log") if isinstance(doc, dict) else None
    if not isinstance(log, dict) or not isinstance(log.get("entries"), list):
        raise ValueError("not a HAR file (no log.entries)")
    for page in log.get("pages") or []:
        if isinstance(page, dict) and isinstance(page.get("title"), str):
            page["title"] = redact_text(page["title"], found)
    for entry in log["entries"]:
        if not isinstance(entry, dict):
            continue
        req = entry.get("request") if isinstance(entry.get("request"), dict) else {}
        if isinstance(req.get("url"), str):
            req["url"] = redact_url(req["url"], found)
        req["headers"] = _headers(req.get("headers"), found)
        if req.get("cookies"):
            found["cookie"] += len(req["cookies"])
        req["cookies"] = []
        req["queryString"] = [{"name": q.get("name", ""), "value": MARK.format("query")}
                              for q in req.get("queryString") or [] if isinstance(q, dict)]
        post = req.get("postData")
        if isinstance(post, dict):
            if post.get("params"):
                found["form value"] += len(post["params"])
                post["params"] = [{"name": p.get("name", ""), "value": MARK.format("form")}
                                  for p in post["params"] if isinstance(p, dict)]
            post["text"] = _body(post.get("text"), post.get("mimeType"), found)
        resp = entry.get("response") if isinstance(entry.get("response"), dict) else {}
        resp["headers"] = _headers(resp.get("headers"), found)
        if resp.get("cookies"):
            found["cookie"] += len(resp["cookies"])
        resp["cookies"] = []
        if isinstance(resp.get("redirectURL"), str) and resp["redirectURL"]:
            resp["redirectURL"] = redact_url(resp["redirectURL"], found)
        content = resp.get("content")
        if isinstance(content, dict) and content.get("text"):
            mime = content.get("mimeType") or ""
            if content.get("encoding") == "base64" or (mime and not TEXT_MIME.match(mime)):
                found["binary response body"] += 1
                content.pop("text", None)
                content.pop("encoding", None)
                content["comment"] = "body removed by TwinLab2: binary content is not shared with the builder"
            else:
                content["text"] = _body(content["text"], mime, found)
        for key in ("_initiator", "_securityDetails", "connection", "serverIPAddress"):
            entry.pop(key, None)
    return _dump(doc)


# -- images -------------------------------------------------------------------------------------

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"sBIT", b"bKGD", b"pHYs"}


def strip_png(data: bytes, found: Counter) -> bytes:
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG image")
    out, pos = [PNG_SIGNATURE], len(PNG_SIGNATURE)
    while pos < len(data):
        if pos + 8 > len(data):
            raise ValueError("truncated PNG")
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        end = pos + 12 + length
        if end > len(data):
            raise ValueError("truncated PNG")
        if kind in PNG_KEEP:
            out.append(data[pos:end])
        else:
            found["image metadata"] += 1
        pos = end
        if kind == b"IEND":
            break
    return b"".join(out)


def strip_jpeg(data: bytes, found: Counter) -> bytes:
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("not a JPEG image")
    out, pos = [b"\xff\xd8"], 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            raise ValueError("malformed JPEG")
        marker = data[pos + 1]
        if marker == 0xDA:  # start of scan: the rest is image data
            out.append(data[pos:])
            return b"".join(out)
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        end = pos + 2 + length
        if end > len(data):
            raise ValueError("truncated JPEG")
        if 0xE1 <= marker <= 0xEF or marker == 0xFE:
            found["image metadata"] += 1
        else:
            out.append(data[pos:end])
        pos = end
    raise ValueError("JPEG without image data")


def strip_image(data: bytes, extension: str, found: Counter) -> bytes:
    if extension == ".png":
        return strip_png(data, found)
    if extension in (".jpg", ".jpeg"):
        return strip_jpeg(data, found)
    raise ValueError("unsupported image type")


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
SHARE_STATES = {
    "restricted": "Restricted evidence (not shared with the builder)",
    "reviewed": "Reviewed by the owner: share, redacted",
    "synthetic": "Synthetic data: share, redacted",
}


def attachment(content: bytes, extension: str, share: str):
    """(bytes for the package or None, outcome, reasons) for one uploaded attachment."""
    found = Counter()
    if share not in ("reviewed", "synthetic"):
        kind = "screenshot" if extension in IMAGE_EXTENSIONS else "attachment"
        return None, "excluded", [f"restricted evidence: the owner has not marked this {kind} reviewed or synthetic"]
    try:
        if extension in IMAGE_EXTENSIONS:
            data = strip_image(content, extension, found)
        elif extension == ".har":
            data = redact_har(content, found)
        elif extension == ".json":
            data = _dump(redact_json(json.loads(content.decode("utf-8-sig")), found))
        else:
            data = redact_text(content.decode("utf-8-sig"), found).encode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        return None, "excluded", [f"could not be checked for sensitive data ({error})"]
    if found:
        return data, "redacted", describe(found)
    return data, "included", []


def describe(found: Counter) -> list[str]:
    return [f"{count} {kind}" for kind, count in sorted(found.items())]
