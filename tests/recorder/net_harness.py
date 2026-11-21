"""Network pieces for the recorder's deployment tests, all on 127.0.0.1 and in this process:

- `ForwardProxy`: an HTTP proxy (absolute-URI requests and CONNECT tunnels), optionally asking
  for Basic credentials (407), recording what went through it;
- `Relay`: a TCP relay in front of a server that can cut every connection in the middle of a
  request (`cut_on`: on the n-th time a byte pattern goes through) and stay down until
  `restore()`, the way a VPN dropping or a Wi-Fi roaming cuts an upload;
- `tls_files`: a private CA (a TLS-inspection root, as an office deploys one) and a server
  certificate for localhost/127.0.0.1 signed by it (cryptography, which moto brings).
"""

from __future__ import annotations

import base64
import socket
import ssl
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit


def _pipe(a: socket.socket, b: socket.socket, on_data=None) -> None:
    def run(src, dst, hook):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                if hook is not None:
                    data = hook(data)
                    if data is None:
                        break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    t = threading.Thread(target=run, args=(b, a, None), daemon=True)
    t.start()
    run(a, b, on_data)
    t.join(5)
    for s in (a, b):
        s.close()


class _Listener:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        self.stopped = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self.stopped:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn: socket.socket) -> None:  # pragma: no cover - overridden
        conn.close()

    def stop(self) -> None:
        self.stopped = True
        self.sock.close()


def _read_head(conn: socket.socket) -> tuple[bytes, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


class ForwardProxy(_Listener):
    def __init__(self, credentials: tuple[str, str] | None = None) -> None:
        self.credentials = credentials
        self.seen: list[tuple[str, str]] = []  # (method, target)
        super().__init__()
        self.url = f"http://127.0.0.1:{self.port}"

    def handle(self, conn: socket.socket) -> None:
        head, rest = _read_head(conn)
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            conn.close()
            return
        headers = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in lines[1:])}
        if self.credentials:
            want = "Basic " + base64.b64encode(":".join(self.credentials).encode()).decode()
            if headers.get("proxy-authorization") != want:
                conn.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic\r\n"
                             b"Content-Length: 0\r\nConnection: close\r\n\r\n")  # fmt: skip
                conn.close()
                return
        self.seen.append((method, target))
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            upstream = socket.create_connection((host.strip("[]"), int(port)), timeout=10)
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
            _pipe(conn, upstream)
            return
        parts = urlsplit(target)
        upstream = socket.create_connection((parts.hostname, parts.port or 80), timeout=10)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        kept = [h for h in lines[1:] if not h.lower().startswith(("proxy-authorization:", "proxy-connection:"))]
        upstream.sendall(f"{method} {path} {version}\r\n".encode("latin-1") + "\r\n".join(kept).encode("latin-1")
                         + b"\r\n\r\n" + rest)  # fmt: skip
        _pipe(conn, upstream)


class Relay(_Listener):
    """127.0.0.1:<port> -> the upstream server, until the network is cut."""

    def __init__(self, upstream_port: int) -> None:
        self.upstream_port = upstream_port
        self.down = False
        self.cut_on: tuple[bytes, int] | None = None  # (pattern, nth): cut when it goes through the nth time
        self.count = 0
        self.lines: list[str] = []  # request lines that went through
        self.conns: list[socket.socket] = []
        self.lock = threading.Lock()
        super().__init__()
        self.url = f"http://127.0.0.1:{self.port}"

    def cut(self) -> None:
        """The network goes away: every open connection is reset; new ones are refused."""
        with self.lock:
            self.down = True
            for c in self.conns:
                try:
                    c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
                    c.close()
                except OSError:
                    pass
            self.conns.clear()

    def restore(self) -> None:
        with self.lock:
            self.down, self.cut_on = False, None

    def handle(self, conn: socket.socket) -> None:
        if self.down:
            conn.close()
            return
        try:
            upstream = socket.create_connection(("127.0.0.1", self.upstream_port), timeout=10)
        except OSError:
            conn.close()
            return
        with self.lock:
            self.conns += [conn, upstream]

        def watch(data: bytes):
            for line in data.split(b"\r\n"):
                if line[:4] in (b"GET ", b"PUT ", b"POST"):
                    self.lines.append(line.decode("latin-1"))
            if self.cut_on and self.cut_on[0] in data:
                self.count += data.count(self.cut_on[0])
                if self.count >= self.cut_on[1]:
                    # half of this request goes through, then the network is gone
                    upstream.sendall(data[: len(data) // 2])
                    threading.Thread(target=self.cut, daemon=True).start()
                    return None
            return None if self.down else data

        _pipe(conn, upstream, watch)


def tls_files(folder: Path) -> dict:
    """{"ca": PEM path, "cert": PEM path, "key": PEM path}: a private root and a localhost server
    certificate it signed."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import ipaddress

    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Office TLS Inspection Root (test)")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
          .not_valid_after(now + timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))  # fmt: skip
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))  # fmt: skip
    out = {"ca": folder / "ca.pem", "cert": folder / "server.pem", "key": folder / "server.key"}
    out["ca"].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    out["cert"].write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    out["key"].write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))  # fmt: skip
    return out


def https_server(handler_cls, tls: dict):
    """A threaded HTTPS server on 127.0.0.1 with the test certificate: (server, port)."""
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tls["cert"], tls["key"])
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]
