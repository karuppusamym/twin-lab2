"""How the recorder reaches the portal through an office network: proxy, certificates, retries.

Proxy, per request URL, in this order:

1. the machine policy's `proxy` (twinlab2_recorder.policy): `direct`, or a proxy URL with its
   `proxy_bypass` list; `system` (the default) goes on to 2;
2. the person's own setting (`--proxy` or settings.json `proxy`), only when the policy says
   `system`;
3. the system's proxy, as Windows resolves it for this user (WinHTTP's
   `WinHttpGetIEProxyConfigForCurrentUser`, the Internet Options / WinINET settings): automatic
   detection (WPAD) or a PAC script (`AutoConfigURL`) are evaluated by Windows itself
   (`WinHttpGetProxyForUrl`), a fixed proxy with its bypass list (`<local>` included) is used as
   it is;
4. `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` (off Windows, and when Windows has no proxy);
5. direct.

Proxy authentication: a proxy URL may carry `user:password@` (Basic). Windows-integrated proxy
authentication (NTLM, Kerberos) is not spoken: the office allows the portal's host through the
proxy without authentication, or names it in the bypass list (docs/recorder-deployment.md).

Certificates: Windows' certificate store (machine and user roots, where a TLS-inspection root
deployed by Group Policy is), plus a PEM bundle when the policy (`ca_bundle`) or the person
(`--ca-bundle`) names one. There is no switch that turns verification off.

Retries: `retrying()` tries a call again when the network failed or the server answered 429,
502, 503 or 504, with exponential backoff and jitter (honouring Retry-After), a few times; the
offline upload queue (twinlab2_recorder.upload_queue) keeps longer waits on disk.
"""

from __future__ import annotations

import base64
import ctypes
import fnmatch
import ipaddress
import random
import ssl
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import unquote, urlsplit
from urllib.request import getproxies_environment, proxy_bypass_environment

RETRY_STATUSES = (429, 502, 503, 504)


# -- proxy strings -----------------------------------------------------------------------------------


def pick_proxy(spec: str, scheme: str) -> str | None:
    """A proxy for `scheme` from a WinHTTP/WinINET proxy string: `host:port`, `a:1;b:2` (the first),
    `http=a:80;https=b:443`, `PROXY a:80; DIRECT` (a PAC answer). None means direct."""
    plain = None
    for part in [p.strip() for p in (spec or "").replace(",", ";").split(";") if p.strip()]:
        word = part.split(None, 1)
        if word[0].upper() == "DIRECT":
            if plain is None:
                return None
            continue
        if word[0].upper() in ("PROXY", "HTTP", "HTTPS") and len(word) == 2:
            part = word[1].strip()
        elif word[0].upper() in ("SOCKS", "SOCKS4", "SOCKS5"):
            continue  # not spoken
        if "=" in part:
            key, _, value = part.partition("=")
            if key.strip().lower() == scheme and value.strip():
                return _proxy_url(value.strip())
            continue
        plain = plain or _proxy_url(part)
    return plain


def _proxy_url(value: str) -> str:
    return value if "://" in value else "http://" + value


def bypassed(host: str, patterns: list[str]) -> bool:
    """Is `host` on a bypass list (`*.corp`, `10.*`, `intranet`, `<local>`: a name without a dot)?"""
    host = (host or "").lower().strip("[]")
    for pattern in patterns:
        p = pattern.strip().lower()
        if not p:
            continue
        if p == "<local>":
            if "." not in host or host in ("localhost", "127.0.0.1", "::1"):
                return True
            continue
        if "/" in p:  # a network: 10.0.0.0/8
            try:
                if ipaddress.ip_address(host) in ipaddress.ip_network(p, strict=False):
                    return True
            except ValueError:
                pass
            continue
        if fnmatch.fnmatchcase(host, p) or (p.startswith(".") and host.endswith(p)) or host == p.lstrip("*."):
            return True
    return False


# -- Windows: WinHTTP ---------------------------------------------------------------------------------


class _IEConfig(ctypes.Structure):
    _fields_ = [("fAutoDetect", ctypes.c_int), ("lpszAutoConfigUrl", ctypes.c_void_p),
                ("lpszProxy", ctypes.c_void_p), ("lpszProxyBypass", ctypes.c_void_p)]  # fmt: skip


class _AutoProxyOptions(ctypes.Structure):
    _fields_ = [("dwFlags", ctypes.c_uint32), ("dwAutoDetectFlags", ctypes.c_uint32),
                ("lpszAutoConfigUrl", ctypes.c_wchar_p), ("lpvReserved", ctypes.c_void_p),
                ("dwReserved", ctypes.c_uint32), ("fAutoLogonIfChallenged", ctypes.c_int)]  # fmt: skip


class _ProxyInfo(ctypes.Structure):
    _fields_ = [("dwAccessType", ctypes.c_uint32), ("lpszProxy", ctypes.c_void_p),
                ("lpszProxyBypass", ctypes.c_void_p)]  # fmt: skip


def _take(ptr) -> str | None:
    """A WinHTTP-allocated string, freed (GlobalFree)."""
    if not ptr:
        return None
    try:
        return ctypes.wstring_at(ptr)
    finally:
        ctypes.windll.kernel32.GlobalFree(ctypes.c_void_p(ptr))


@dataclass
class SystemProxy:
    auto_detect: bool = False
    pac_url: str | None = None
    proxy: str | None = None
    bypass: str | None = None


@lru_cache(maxsize=1)
def _ie_config_cached(_minute: int) -> SystemProxy:
    winhttp = ctypes.windll.winhttp
    cfg = _IEConfig()
    if not winhttp.WinHttpGetIEProxyConfigForCurrentUser(ctypes.byref(cfg)):
        return SystemProxy()
    return SystemProxy(bool(cfg.fAutoDetect), _take(cfg.lpszAutoConfigUrl), _take(cfg.lpszProxy),
                       _take(cfg.lpszProxyBypass))  # fmt: skip


def system_proxy_config() -> SystemProxy:
    """This user's Internet Options proxy settings (read again every minute)."""
    if sys.platform != "win32":
        return SystemProxy()
    try:
        return _ie_config_cached(int(time.time() // 60))
    except (OSError, AttributeError):
        return SystemProxy()


_pac_cache: dict[tuple, tuple[float, str | None]] = {}


def winhttp_proxy_for_url(url: str, pac_url: str | None, auto_detect: bool) -> str | None:
    """What Windows' PAC/WPAD evaluation says for `url`: a WinHTTP proxy string, or None (direct,
    or no answer). Cached per host for five minutes."""
    parts = urlsplit(url)
    key = (parts.scheme, parts.netloc, pac_url, auto_detect)
    hit = _pac_cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    winhttp = ctypes.windll.winhttp
    winhttp.WinHttpOpen.restype = ctypes.c_void_p
    session = winhttp.WinHttpOpen("TwinLabRecorder", 1, None, None, 0)  # NO_PROXY: only for the lookup
    answer = None
    if session:
        try:
            opts = _AutoProxyOptions()
            if pac_url:
                opts.dwFlags = 0x2  # WINHTTP_AUTOPROXY_CONFIG_URL
                opts.lpszAutoConfigUrl = pac_url
            else:
                opts.dwFlags = 0x1  # WINHTTP_AUTOPROXY_AUTO_DETECT
                opts.dwAutoDetectFlags = 0x1 | 0x2  # DHCP | DNS_A
            opts.fAutoLogonIfChallenged = 1
            info = _ProxyInfo()
            get = winhttp.WinHttpGetProxyForUrl
            get.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(_AutoProxyOptions),
                            ctypes.POINTER(_ProxyInfo)]  # fmt: skip
            if get(session, url, ctypes.byref(opts), ctypes.byref(info)):
                proxy, _bypass = _take(info.lpszProxy), _take(info.lpszProxyBypass)
                answer = proxy if info.dwAccessType == 3 else None  # NAMED_PROXY
        finally:
            winhttp.WinHttpCloseHandle(ctypes.c_void_p(session))
    _pac_cache[key] = (time.monotonic() + 300, answer)
    return answer


# -- the network settings of the recorder ---------------------------------------------------------------


@dataclass
class Network:
    """Proxy and certificates for every call the recorder makes (twinlab2_recorder.client)."""

    proxy: str = "system"  # system | direct | a proxy URL
    bypass: tuple[str, ...] = ()
    ca_bundle: str | None = None
    source: str = "default"  # where the proxy setting came from: policy | user | default

    @classmethod
    def resolve(cls, ca_bundle: str | None = None, proxy: str | None = None, policy=None) -> Network:
        """The policy's settings over the person's (`ca_bundle`, `proxy`)."""
        if policy is None:
            from twinlab2_recorder import policy as machine

            policy = machine.load()
        net = cls(ca_bundle=policy.ca_bundle or ca_bundle)
        if policy.proxy != "system":
            net.proxy, net.bypass, net.source = policy.proxy, tuple(policy.proxy_bypass), "policy"
        elif proxy and proxy.strip().lower() != "system":
            net.proxy = proxy.strip().lower() if proxy.strip().lower() == "direct" else _proxy_url(proxy.strip())
            net.bypass, net.source = tuple(policy.proxy_bypass), "user"
        else:
            net.bypass = tuple(policy.proxy_bypass)
        return net

    def proxy_for(self, url: str) -> str | None:
        """The proxy URL for a request to `url`, or None (direct)."""
        parts = urlsplit(url)
        host, scheme = (parts.hostname or "").lower(), parts.scheme
        if host in ("localhost", "127.0.0.1", "::1") and self.proxy == "system":
            return None  # this machine never goes through the system's proxy
        if self.bypass and bypassed(host, list(self.bypass)):
            return None
        if self.proxy == "direct":
            return None
        if self.proxy != "system":
            return self.proxy
        sysp = system_proxy_config()
        if sysp.auto_detect or sysp.pac_url:
            try:
                got = winhttp_proxy_for_url(url, sysp.pac_url, sysp.auto_detect)
            except (OSError, AttributeError):
                got = None
            if got:
                return pick_proxy(got, scheme)
        if sysp.proxy:
            if sysp.bypass and bypassed(host, sysp.bypass.replace(" ", ";").split(";")):
                return None
            return pick_proxy(sysp.proxy, scheme)
        env = getproxies_environment()
        if env.get(scheme) and not proxy_bypass_environment(host, env):
            return env[scheme]
        return None

    def ssl_context(self) -> ssl.SSLContext:
        """Windows' store (or the system's), plus the CA bundle when there is one."""
        context = ssl.create_default_context()
        if self.ca_bundle:
            context.load_verify_locations(cafile=self.ca_bundle)
        return context

    def describe(self) -> str:
        bits = [f"proxy {self.proxy} ({self.source})"]
        if self.bypass:
            bits.append("bypass " + ";".join(self.bypass))
        if self.ca_bundle:
            bits.append(f"extra CAs {self.ca_bundle}")
        return ", ".join(bits)


def apply_proxy(request, proxy: str | None) -> None:
    """Send a urllib Request through `proxy` (HTTPS through a CONNECT tunnel); Basic credentials
    from the proxy URL."""
    if not proxy:
        return
    parts = urlsplit(proxy)
    hostport = parts.hostname + (f":{parts.port}" if parts.port else "")
    if ":" in (parts.hostname or "") and not hostport.startswith("["):
        hostport = f"[{parts.hostname}]" + (f":{parts.port}" if parts.port else "")
    if parts.username:
        creds = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()
        request.add_header("Proxy-Authorization", "Basic " + base64.b64encode(creds).decode())
    request.set_proxy(hostport, request.type)


# -- retries ----------------------------------------------------------------------------------------------


def backoff(attempt: int, base: float = 0.5, cap: float = 8.0, retry_after: float | None = None) -> float:
    """Seconds to wait before attempt `attempt + 1` (full jitter; Retry-After when it is longer)."""
    wait = random.uniform(0, min(cap, base * 2 ** attempt))
    if retry_after is not None:
        wait = max(wait, min(retry_after, 60.0))
    return wait


def retry_after_of(headers) -> float | None:
    value = (headers or {}).get("Retry-After") if headers is not None else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def retrying(call, attempts: int = 3, sleep=time.sleep, retryable=None):
    """Run `call()`; when it raises an error `retryable(error)` accepts, wait and try again, up to
    `attempts` times in all. `retryable` returns False, or True / a Retry-After in seconds."""
    for attempt in range(attempts):
        try:
            return call()
        except Exception as error:  # noqa: BLE001 - retryable decides
            verdict = retryable(error) if retryable else False
            if verdict is False or verdict is None or attempt == attempts - 1:
                raise
            sleep(backoff(attempt, retry_after=None if verdict is True else float(verdict)))
    raise AssertionError("unreachable")
