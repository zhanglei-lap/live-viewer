from __future__ import annotations

import copy
import io
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from email.parser import Parser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request

import requests

from .core import COOKIE_DOMAINS, ViewerError, belongs_to, new_session
from .network import connection_message
from .platforms import require_spec


def login_url(url: str, platform: str):
    spec = require_spec(platform)
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (ValueError, TypeError):
        raise ViewerError("平台返回的登录地址格式异常。") from None
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or port not in (None, 443) or len(url) > 4096
            or not belongs_to(parsed.hostname or "", spec.cookie_domains)):
        raise ViewerError("平台返回了不支持的登录地址，请重新生成二维码。")


def qr_png(url: str) -> bytes:
    try:
        import qrcode
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=5, border=4)
        qr.add_data(url)
        qr.make(fit=True)
        buffer = io.BytesIO()
        qr.make_image(fill_color="black", back_color="white").save(buffer, format="PNG")
        return buffer.getvalue()
    except ImportError:
        raise ViewerError("缺少二维码组件，请使用新版 EXE，或安装 requirements.txt 中的依赖。") from None


class LoginHttp:
    """Platform-only HTTPS, with Windows system curl as a TLS compatibility fallback."""

    def __init__(self, platform, *, network_mode="direct"):
        self.platform = platform
        self.session = new_session(network_mode=network_mode)
        self.session.headers.update({"Referer": f"https://passport.{platform}.com/",
                                     "Origin": f"https://passport.{platform}.com",
                                     "X-Requested-With": "XMLHttpRequest"})
        self.windows_tls = False

    def _curl(self, method, url, data):
        binary = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
        if os.name != "nt" or not binary.is_file():
            raise ViewerError("登录服务器的 HTTPS 连接失败，请检查网络后重试。")
        prepared = self.session.prepare_request(requests.Request(method, url, data=data))

        def quote(value):
            return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n") + '"'

        # URL keys, request bodies and cookies go via stdin, never the command line.
        config = ["url = " + quote(prepared.url), "request = " + quote(method)]
        proxies = self.session.merge_environment_settings(prepared.url, {}, False, None, None)["proxies"]
        selected_proxy = requests.utils.select_proxy(prepared.url, proxies)
        if selected_proxy:
            config += ["proxy = " + quote(selected_proxy), 'noproxy = ""']
        else:
            config += ['proxy = ""', 'noproxy = "*"']
        config += ["header = " + quote(f"{k}: {v}") for k, v in prepared.headers.items() if k.lower() != "content-length"]
        if prepared.body is not None:
            body = prepared.body.decode("utf-8") if isinstance(prepared.body, bytes) else prepared.body
            config.append("data = " + quote(body))
        try:
            arguments = [str(binary), "--disable", "--config", "-", "--silent", "--show-error", "--include",
                         "--max-time", "15", "--proto", "=https"]
            families = [["--ipv4"], []] if not self.session.trust_env else [[]]
            for family in families:
                result = subprocess.run(arguments + family,
                                        input="\n".join(config).encode("utf-8"), capture_output=True, timeout=18,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode not in (6, 7, 28):
                    break
        except (OSError, subprocess.TimeoutExpired):
            raise ViewerError("登录服务器连接超时，请重试。") from None
        if result.returncode:
            raise ViewerError("登录服务器的 HTTPS 连接失败，请检查网络后重试。")
        payload, headers, status = result.stdout, None, None
        while payload.startswith(b"HTTP/"):
            block, separator, payload = payload.partition(b"\r\n\r\n")
            if not separator:
                raise ViewerError("登录服务器返回格式异常。")
            first, _, lines = block.partition(b"\r\n")
            try:
                status = int(first.split()[1])
                headers = Parser().parsestr(lines.decode("iso-8859-1"))
            except (ValueError, IndexError):
                raise ViewerError("登录服务器返回格式异常。") from None
        if headers is None or status is None:
            raise ViewerError("登录服务器返回格式异常。")

        class CookieResponse:
            def info(self): return headers
        self.session.cookies.extract_cookies(CookieResponse(), Request(url))
        return status, dict(headers.items()), payload

    def request(self, method, url, *, data=None):
        for _ in range(6):
            login_url(url, self.platform)
            if not self.windows_tls:
                try:
                    with self.session.request(method, url, data=data, allow_redirects=False, timeout=(6, 12)) as response:
                        status, headers, payload = response.status_code, dict(response.headers), response.content
                except requests.exceptions.SSLError:
                    self.windows_tls = True
                except requests.RequestException as exc:
                    raise ViewerError(connection_message(exc, "扫码登录接口", url, self.session.viewer_network_mode)) from None
            if self.windows_tls:
                status, headers, payload = self._curl(method, url, data)
            if status in (301, 302, 303, 307, 308):
                target = next((v for k, v in headers.items() if k.lower() == "location"), "")
                if not target:
                    raise ViewerError("登录接口返回无效重定向。")
                url = urljoin(url, target)
                if status in (301, 302, 303):
                    method, data = "GET", None
                continue
            if status != 200 or len(payload) > 1024 * 1024:
                raise ViewerError(f"登录接口返回 HTTP {status} 或异常数据，请稍后重试。")
            try:
                text = payload.decode("utf-8-sig")
                # Only this callback wrapper is accepted; never execute returned JS.
                match = re.fullmatch(r"\s*appClient_json_callback\((.*)\)\s*;?\s*", text, re.S)
                return json.loads(match[1] if match else text)
            except (ValueError, UnicodeError):
                raise ViewerError("登录接口返回格式发生变化，请改用 Cookie 导入。") from None
        raise ViewerError("登录接口重定向次数过多。")

    def close(self):
        self.session.cookies.clear()
        self.session.close()


@dataclass(frozen=True)
class Challenge:
    png: bytes = field(repr=False)
    expires_seconds: int = 180


@dataclass(frozen=True)
class LoginState:
    state: str
    cookies: requests.cookies.RequestsCookieJar | None = field(default=None, repr=False)


class QrLogin:
    def __init__(self, platform, http=None, *, network_mode="direct"):
        spec = require_spec(platform)
        if not spec.qr_login:
            raise ViewerError(f"{spec.caption}暂不支持扫码登录，请改用“粘贴 Cookie”或导入 Cookie 文件。",
                              code="account.no_qr", level="WARN")
        self.platform, self.spec = platform, spec
        self.http = http or LoginHttp(platform, network_mode=network_mode)
        self.cancelled = threading.Event()
        self.code = None
        self.finished = False

    def create(self) -> Challenge:
        if self.platform == "bilibili":
            response = self.http.request("GET", "https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
            if response.get("code") != 0:
                raise ViewerError("B 站二维码申请失败，请稍后重试。")
            data = response["data"]
            self.code = data["qrcode_key"]
            ttl = 180
        elif self.platform == "douyu":
            response = self.http.request("POST", "https://passport.douyu.com/scan/generateCode",
                                         data={"client_id": "1", "isMultiAccount": "0"})
            if response.get("error") != 0:
                raise ViewerError("斗鱼二维码申请失败，请稍后重试。")
            data = response["data"]
            self.code = data["code"]
            ttl = int(data.get("expire") or 180)
            ttl = ttl if 10 <= ttl <= 600 else 180
        else:
            raise ViewerError("该平台的扫码流程尚未实现，请改用 Cookie 导入。", code="account.no_qr")
        if not isinstance(self.code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", self.code):
            raise ViewerError("平台二维码信息格式异常。")
        login_url(data["url"], self.platform)
        return Challenge(qr_png(data["url"]), ttl)

    def poll(self) -> LoginState:
        if self.cancelled.is_set():
            return LoginState("cancelled")
        if self.platform == "bilibili":
            response = self.http.request("GET", "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?" + urlencode({"qrcode_key": self.code}))
            if response.get("code") != 0:
                raise ViewerError("B 站扫码状态查询失败，请刷新二维码。")
            status = response["data"]["code"]
            states = {86101: "waiting", 86090: "scanned", 86038: "expired"}
        elif self.platform == "douyu":
            response = self.http.request("GET", "https://passport.douyu.com/japi/scan/auth?" + urlencode({"code": self.code, "time": int(time.time() * 1000)}))
            status = response.get("error")
            states = {-2: "waiting", 1: "scanned", -1: "expired"}
        else:
            raise ViewerError("该平台的扫码流程尚未实现，请改用 Cookie 导入。", code="account.no_qr")
        if self.cancelled.is_set():
            return LoginState("cancelled")
        if status in states:
            return LoginState(states[status])
        if status != 0:
            raise ViewerError("平台返回未知扫码状态，请刷新二维码或使用 Cookie 导入。")
        if self.platform == "douyu":
            target = response["data"]["url"]
            login_url(target, self.platform)
            separator = "&" if "?" in target else "?"
            result = self.http.request("GET", target + separator + "callback=appClient_json_callback")
            if result.get("error") != 0:
                raise ViewerError("斗鱼扫码已确认，但登录回调失败，请重新扫码或导入 Cookie。")
        if self.cancelled.is_set():
            return LoginState("cancelled")
        jar = requests.cookies.RequestsCookieJar()
        for cookie in self.http.session.cookies:
            if belongs_to(cookie.domain, COOKIE_DOMAINS[self.platform]) and not cookie.is_expired():
                jar.set_cookie(copy.copy(cookie))
        required = self.spec.qr_credentials
        if not any(c.name in required and c.value for c in jar):
            raise ViewerError("平台已确认扫码，但未收到登录 Cookie；请刷新二维码或改用 Cookie 导入。")
        self.finished = True
        return LoginState("success", jar)

    def cancel(self):
        self.cancelled.set()

    def close(self):
        if self.spec.qr_cancel_url and self.code and not self.finished:
            try:
                self.http.request("POST", self.spec.qr_cancel_url, data={"code": self.code})
            except Exception:
                pass
        self.code = None
        self.http.close()
