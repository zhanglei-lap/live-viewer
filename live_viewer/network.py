from __future__ import annotations

import socket
import sys
from urllib.parse import urlparse

import requests
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NameResolutionError, NewConnectionError
from urllib3.util import Timeout

NETWORK_MODES = {"直连（推荐）": "direct", "系统 / 环境代理": "system"}


class IPv4FirstConnection:
    """Change TCP address ordering only; inherited HTTPS keeps SNI and verification."""

    def _new_conn(self):
        try:
            addresses = socket.getaddrinfo(self._dns_host, self.port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise NameResolutionError(self.host, self, exc) from exc
        addresses.sort(key=lambda address: address[0] != socket.AF_INET)
        last_error = OSError("No usable DNS address")
        for family, kind, protocol, _, address in addresses:
            connection = None
            try:
                connection = socket.socket(family, kind, protocol)
                connection.settimeout(Timeout.resolve_default_timeout(self.timeout))
                for level, option, value in self.socket_options or []:
                    connection.setsockopt(level, option, value)
                if self.source_address:
                    connection.bind(self.source_address)
                connection.connect(address)
                sys.audit("http.client.connect", self, self.host, self.port)
                return connection
            except OSError as exc:
                last_error = exc
                if connection is not None:
                    connection.close()
        if isinstance(last_error, TimeoutError):
            raise ConnectTimeoutError(self, "TCP connection timed out") from last_error
        raise NewConnectionError(self, "TCP connection failed") from last_error


class IPv4HTTPConnection(IPv4FirstConnection, HTTPConnection):
    pass


class IPv4HTTPSConnection(IPv4FirstConnection, HTTPSConnection):
    pass


class IPv4HTTPPool(HTTPConnectionPool):
    ConnectionCls = IPv4HTTPConnection


class IPv4HTTPSPool(HTTPSConnectionPool):
    ConnectionCls = IPv4HTTPSConnection


class IPv4FirstAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        # Pool classes are per-manager, never mutate urllib3's global registry.
        self.poolmanager.pool_classes_by_scheme = {"http": IPv4HTTPPool, "https": IPv4HTTPSPool}


def configure_session(session, mode="direct"):
    # Per-session policy: do not change the host's proxy or environment.
    session.trust_env = mode == "system"
    session.proxies.clear()
    session.viewer_network_mode = "system" if mode == "system" else "direct"
    if not session.trust_env:
        for scheme in ("http://", "https://"):
            session.mount(scheme, IPv4FirstAdapter())
    return session


def connection_kind(exc):
    if isinstance(exc, requests.exceptions.ProxyError):
        category, code = "代理连接失败", "network.proxy_failed"
    elif isinstance(exc, requests.exceptions.SSLError):
        category, code = "TLS / 证书连接失败", "network.tls_failed"
    elif isinstance(exc, requests.exceptions.Timeout):
        category, code = "连接或读取超时", "network.timeout"
    else:
        category, code = "连接失败", "network.connection_failed"
        seen, pending = set(), [exc]
        while pending and len(seen) < 24:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            if isinstance(current, socket.gaierror) or type(current).__name__ == "NameResolutionError":
                category, code = "DNS 解析失败", "network.dns_failed"
                break
            for nested in [getattr(current, "reason", None), current.__cause__, current.__context__, *current.args]:
                if isinstance(nested, BaseException):
                    pending.append(nested)
    return category, code


def connection_message(exc, stage, url, mode="direct"):
    """Only export the stage, exception category and public DNS name."""
    host = urlparse(url).hostname or "未知主机"
    category, _ = connection_kind(exc)
    route = "直连" if mode != "system" else "系统 / 环境代理"
    return f"{stage}：{category}（{host}；{route}）。请检查网络设置，或运行连接检查。"
