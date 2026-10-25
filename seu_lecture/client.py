"""统一身份认证（CAS）登录与讲座业务接口。

设计原则：
1. 只用白名单域名的 HTTPS 请求，避免把凭据发到意外主机。
2. 登录流程走学校 self.ep.cas + 网关票据，登录一次后由 Session 自动维持 Cookie。
3. 业务接口存在新旧两种风格，启动时自动探测，避免写死一个可能已下线的地址。
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import requests
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import load_der_public_key

from . import log

API_PARAMS = {"clientType": "SDPBrowserClient", "platform": "Linux", "lang": "zh-CN"}

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
TIMEOUT = (4, 12)


@dataclass(frozen=True)
class Endpoints:
    """一所学校的接入地址，全部来自配置，代码里不留任何学校常量。"""

    entry: str  # 教务系统微应用入口，未登录时会跳转统一认证
    cas: str  # 统一认证接口前缀，以 / 结尾
    gateway: str = ""  # 认证网关，留空则跳过网关直接用 CAS
    gateway_ip: str = ""  # 网关域名的备用 IP，留空则常规 DNS
    api_bases: tuple[str, ...] = ()  # 额外候选的业务接口基址

    @property
    def hosts(self) -> set[str]:
        names: set[str] = set()
        for url in (self.entry, self.cas, self.gateway, *self.api_bases):
            host = urlsplit(url).hostname
            if host:
                names.add(host)
        return names

    @property
    def app_base(self) -> str:
        """从入口地址推导应用基址：把 /*default/index.do 之前的路径留下。"""
        parts = urlsplit(self.entry)
        path = parts.path.split("/*default")[0].rstrip("/")
        return f"{parts.scheme}://{parts.netloc}{path}"


def _app_name(base: str) -> str:
    return urlsplit(base).path.rstrip("/").rpartition("/")[2] or "app"


# 提交结果的语义化分类
# 提交结果的语义化分类
SUCCESS = "success"
DUPLICATE = "duplicate"
FULL = "full"
BUSY = "busy"
EXPIRED = "expired"
NOT_OPEN = "not_open"
CAPTCHA_WRONG = "captcha"
FAILED = "failed"

_FULL_WORDS = ("已满", "名额已满", "人数已满", "已达上限", "预约已满")
_BUSY_WORDS = ("繁忙", "稍后", "请重试", "过于频繁")
_DUP_WORDS = ("已预约", "不能重复", "重复预约", "已经预约")
_CAPTCHA_WORDS = ("验证码", "vcode")
# 服务器时间往往比列表标注的开放时刻晚一点，这时会返回这个
_NOT_OPEN_WORDS = ("尚未开放", "未开放", "未开始", "未到预约", "还没开始")


class LoginError(Exception):
    """登录失败，需要人工介入或检查账号。"""


class SessionExpired(Exception):
    """会话已失效，需要重新登录。"""


class ApiError(Exception):
    """业务接口不可用或响应异常。"""


@dataclass(frozen=True)
class ApiProfile:
    """一种业务接口风格。"""

    name: str
    base: str
    list_path: str
    list_method: str
    vcode_path: str
    submit_path: str
    submit_style: str  # paramjson | form

    @property
    def list_url(self) -> str:
        return self.base + self.list_path

    @property
    def vcode_url(self) -> str:
        return self.base + self.vcode_path

    @property
    def submit_url(self) -> str:
        return self.base + self.submit_path


def build_profiles(endpoints: Endpoints) -> tuple[ApiProfile, ...]:
    """为每个基址生成两种常见风格的候选接口，启动时逐个探测。"""
    bases: list[str] = [endpoints.app_base]
    for extra in endpoints.api_bases:
        if extra not in bases:
            bases.append(extra)

    profiles: list[ApiProfile] = []
    for base in bases:
        name = _app_name(base)
        profiles.append(
            ApiProfile(
                name, base,
                "/hdyy/queryActivityList.do", "GET",
                "/hdyy/vcode.do", "/hdyy/yySave.do", "paramjson",
            )
        )
        profiles.append(
            ApiProfile(
                f"{name}/modules", base,
                "/modules/hdyy/queryActivityList.do", "POST",
                "/modules/hdyy/vcode.do", "/modules/hdyy/addReservation.do", "form",
            )
        )
    return tuple(profiles)


def _allowed(url: str, hosts: set[str]) -> bool:
    """只放行配置里声明的域名；不改写协议，避免破坏 CAS ticket 的 service 校验。"""
    parts = urlsplit(url)
    if parts.username or parts.password or not parts.hostname:
        return False
    if parts.hostname not in hosts:
        return False
    if parts.scheme == "https":
        return parts.port in (None, 443)
    if parts.scheme == "http":
        return parts.port in (None, 80)
    return False


def _service_of(url: str) -> str:
    """从登录页 URL 的 query 或 fragment 里取出 service 参数。"""
    parts = urlsplit(url)
    for scope in (parts.query, parts.fragment):
        if not scope:
            continue
        query = scope.split("?", 1)[1] if "?" in scope else scope
        values = parse_qs(query).get("service") or [""]
        if values[0]:
            return values[0]
    raise LoginError("登录页未携带 service 参数，无法继续 CAS 认证")


def _succeeded(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("success") in (True, 1, "1", "true", "True"):
        return True
    code = payload.get("code")
    return type(code) is int and code in (0, 200)


def _extract_rows(payload: Any) -> list[dict[str, Any]] | None:
    """兼容多种列表返回结构；无法识别时返回 None。"""
    keys = ("hdlbList", "wdhdList", "rows", "dataList", "data")
    candidates: list[Any] = []

    if isinstance(payload, list):
        candidates.append(payload)
    elif isinstance(payload, dict):
        datas = payload.get("datas")
        for source in (payload, datas):
            if isinstance(source, dict):
                candidates.extend(source.get(key) for key in keys)
            elif isinstance(source, list):
                candidates.append(source)

    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = next(
                (candidate[key] for key in keys if isinstance(candidate.get(key), list)), None
            )
        if not isinstance(candidate, list):
            continue
        if all(isinstance(row, dict) for row in candidate):
            return candidate
    return None


def _data_url(value: Any) -> str | None:
    """在嵌套响应里递归寻找 data:image/ base64 串。"""
    if isinstance(value, str):
        return value if value.startswith("data:image/") else None
    if isinstance(value, dict):
        for child in value.values():
            found = _data_url(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _data_url(child)
            if found:
                return found
    return None


def _where(url: str) -> str:
    """只打印主机与路径，避免把 ticket、query 之类刷到日志里。"""
    parts = urlsplit(url)
    return f"{parts.hostname or '?'}{parts.path}"


def _visible_text(html: str, limit: int = 220) -> str:
    """压缩 HTML 成一行可读文本，仅用于诊断。"""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] or "（空）"


class GatewayIPAdapter(requests.adapters.HTTPAdapter):
    """把发往网关域名的请求定向到固定 IP，同时保留域名做 Host 头与 TLS SNI。

    学校网关域名在部分网络下没有 DNS 记录，只能靠 IP 直连；
    域名证书仍按原域名校验，不会降低安全性。
    """

    def __init__(self, address: str, hostname: str) -> None:
        self.address = str(ip_address(address))
        self.hostname = hostname
        super().__init__()

    def build_connection_pool_key_attributes(self, request: Any, verify: Any,
                                             cert: Any = None) -> Any:
        host, tls = super().build_connection_pool_key_attributes(request, verify, cert)
        tls["server_hostname"] = host["host"]
        host["host"] = self.address
        return host, tls

    def add_headers(self, request: Any, **kwargs: Any) -> None:
        request.headers["Host"] = self.hostname


class LectureClient:
    def __init__(self, username: str, password: str, endpoints: Endpoints, *,
                 use_proxy: bool = False) -> None:
        self.username = username
        self.password = password
        self.ep = endpoints
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        # 学校系统大多公网可达；默认绕开系统代理，避免 Clash/V2Ray 之类把校园域名打到 502
        self.session.trust_env = use_proxy
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
        self.session.mount("https://", adapter)
        if endpoints.gateway:
            gateway_base = endpoints.gateway.rstrip("/")
            gateway_host = urlsplit(gateway_base).hostname or ""
            if endpoints.gateway_ip and gateway_host:
                try:
                    gateway_adapter = GatewayIPAdapter(endpoints.gateway_ip, gateway_host)
                except ValueError:
                    log.warn(f"SEU_GATEWAY_IP 不是合法 IP（{endpoints.gateway_ip}），改为常规 DNS 解析")
                    gateway_adapter = requests.adapters.HTTPAdapter(
                        pool_connections=16, pool_maxsize=16
                    )
            else:
                gateway_adapter = requests.adapters.HTTPAdapter(
                    pool_connections=16, pool_maxsize=16
                )
            self.session.mount(gateway_base + "/", gateway_adapter)
            self.session.mount(gateway_base + ":443/", gateway_adapter)
        self.profile: ApiProfile | None = None
        self._login_lock = threading.Lock()
        self.login_count = 0

    @property
    def _entry_host(self) -> str:
        return urlsplit(self.ep.entry).hostname or ""

    @property
    def _cas_host(self) -> str:
        return urlsplit(self.ep.cas).hostname or ""

    # ── 底层请求 ────────────────────────────────────────────

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        if not _allowed(url, self.ep.hosts):
            raise ApiError(f"拒绝访问未在配置中声明的地址：{_where(url)}")
        if "timeout" not in kwargs:
            kwargs["timeout"] = TIMEOUT
        try:
            return self.session.request(method, url, allow_redirects=False, **kwargs)
        except requests.exceptions.ProxyError:
            raise ApiError(
                "系统代理拦下了请求。请在 .env 设置 SEU_USE_PROXY=false，或关闭代理软件"
            ) from None
        except requests.exceptions.SSLError as exc:
            raise ApiError(f"TLS 握手失败（可能被代理或防火墙干扰）：{exc}") from None
        except requests.exceptions.Timeout:
            raise ApiError("请求超时，请检查网络稳定性")
        except requests.exceptions.ConnectionError:
            raise ApiError("无法连接服务器，请检查网络与域名配置")
        except requests.exceptions.RequestException as exc:
            raise ApiError(f"网络请求失败：{type(exc).__name__}") from None

    def _follow(self, url: str, *, max_hops: int = 12) -> requests.Response:
        """手动跟随跳转，便于读取中间 Location。"""
        for _ in range(max_hops):
            response = self._request("GET", url)
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("Location")
            if not location:
                raise ApiError("跳转响应缺少 Location")
            url = urljoin(url, location)
        raise ApiError("跳转次数过多，疑似循环重定向")

    @staticmethod
    def _payload(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            raise ApiError(f"响应不是 JSON（HTTP {response.status_code}）") from None

    # ── 登录 ────────────────────────────────────────────────

    def login(self) -> None:
        """完整登录一次；失败抛 LoginError。"""
        with self._login_lock:
            try:
                self._login_flow()
            except LoginError:
                raise
            except Exception as exc:
                raise LoginError(f"登录过程异常：{exc}") from exc
            self.login_count += 1
            log.info(f"登录成功（第 {self.login_count} 次）")

    def _app_ready(self, response: requests.Response) -> bool:
        parts = urlsplit(response.url)
        return (
            parts.hostname == self._entry_host
            and response.status_code == 200
            and "indexjsp.js" in response.text
        )

    def _login_flow(self) -> None:
        app = self._follow(self.ep.entry)
        log.info(f"入口 → HTTP {app.status_code} {_where(app.url)}（{len(app.text)} 字节）")
        if self._app_ready(app):
            log.info("会话仍然有效，跳过登录")
            return

        # 学校的统一认证走 vpn 网关签发，直接用应用自身地址换 service 只能拿到无效会话，
        # 所以优先走网关；网关不可达时才退回直接 CAS。
        try:
            self._gateway_login()
        except Exception as exc:
            log.warn(f"网关认证失败：{exc}")
            log.info("回退为直接 CAS 登录")
            try:
                self._direct_login()
            except Exception as direct_exc:
                log.warn(f"直接 CAS 也失败：{direct_exc}")
        else:
            log.info("网关认证步骤完成")

        app = self._resolve_app()
        if not self._app_ready(app):
            log.warn(
                f"登录后仍未进入应用页面：HTTP {app.status_code} {_where(app.url)}，"
                f"{len(app.text)} 字节，含 indexjsp.js={'indexjsp.js' in app.text}"
            )
            log.warn(f"页面片段：{_visible_text(app.text)}")

    def _resolve_app(self) -> requests.Response:
        """访问登录入口；若应用要求单独授权则补一次 verifyTgt。"""
        app = self._follow(self.ep.entry)
        if self._app_ready(app):
            return app
        if urlsplit(app.url).hostname != self._cas_host:
            return app

        log.info("应用要求单独授权，尝试 verifyTgt")
        try:
            verified = self._payload(
                self._request(
                    "POST",
                    self.ep.cas + "verifyTgt",
                    json={"service": _service_of(app.url), "loginType": "account"},
                )
            )
        except Exception as exc:
            log.warn(f"verifyTgt 请求失败：{exc}")
            return app
        if verified.get("code") == 201 and isinstance(verified.get("redirectUrl"), str):
            return self._follow(self._callback_url(verified["redirectUrl"]))
        log.warn(f"verifyTgt 未通过：code={verified.get('code')} {verified.get('info') or ''}")
        return app

    def _direct_login(self) -> None:
        """直接用应用地址作 service 做 CAS 登录。"""
        app = self._follow(self.ep.entry)
        service = _service_of(app.url)
        log.info(f"直接 CAS，service：{service}")
        redirect = self._cas_login(service)
        target = self._callback_url(redirect)
        landed = self._follow(target)
        log.info(f"ticket 回调 → HTTP {landed.status_code} {_where(landed.url)}")
        log.info(f"当前 Cookie 域：{sorted({c.domain for c in self.session.cookies}) or '无'}")

    def _callback_url(self, redirect: str) -> str:
        """casLogin 返回的 redirectUrl 可能是编码过的绝对地址或相对路径。"""
        lowered = redirect.lower()
        if lowered.startswith(("http%3a", "https%3a")):
            return unquote(redirect)
        if redirect.startswith(("http://", "https://")):
            return redirect
        return self.ep.cas + "loginRedirect?redirectUrl=" + quote(redirect, safe="")

    def _gateway_login(self) -> None:
        config_url = self.ep.gateway + "/passport/v1/public/authConfig"
        self._payload(
            self._request("GET", config_url, params={**API_PARAMS, "mod": 1, "needTicket": 1})
        )
        log.info("网关配置已获取")

        # 网关的 casLogin 会 302 到 CAS，其 URL 里带着网关要求的 service
        auth_page = self._follow(self.ep.gateway + "/passport/v1/public/casLogin?sfDomain=CAS-auth")
        service = _service_of(auth_page.url)
        log.info(f"网关 service：{service[:80]}")

        redirect = self._cas_login(service)
        # 这里必须保持 CAS 的 loginRedirect 中转，不能直接解码访问
        callback = self._follow(self.ep.cas + "loginRedirect?redirectUrl=" + redirect)
        log.info(f"网关回调 → {_where(callback.url)}")

        query = parse_qs(urlsplit(callback.url).query)
        try:
            data = json.loads((query.get("data") or ["{}"])[0])
        except ValueError:
            data = {}

        config = self._payload(
            self._request("GET", config_url, params={**API_PARAMS, "mod": 1, "needTicket": 1})
        )
        try:
            headers = {
                "x-csrf-token": config["data"]["security"]["csrfToken"],
                "x-sdp-traceid": uuid.uuid4().hex,
                "Referer": callback.url,
            }
        except (KeyError, TypeError):
            raise ApiError("网关认证配置缺少 csrfToken") from None

        if isinstance(data, dict) and data.get("env", {}).get("need") and data.get("ticket"):
            self._report_env(data["ticket"], headers)

        next_service = (query.get("nextService") or [""])[0]
        for _ in range(4):
            if not next_service:
                break
            if next_service not in ("auth/authCheck", "auth/accessCheck"):
                raise LoginError(f"网关要求额外认证步骤：{next_service}")
            params = dict(API_PARAMS)
            if query.get("taskId"):
                params["taskId"] = query["taskId"][0]
            method = "GET" if next_service.endswith("authCheck") else "POST"
            checked = self._payload(
                self._request(
                    method,
                    self.ep.gateway + "/passport/v1/" + next_service,
                    params=params,
                    headers=headers,
                )
            )
            if not _succeeded(checked):
                raise LoginError("网关拒绝了本次访问")
            next_service = (checked.get("data") or {}).get("nextService") or ""
        if next_service:
            raise LoginError("网关认证步骤未走完")

        config = self._payload(
            self._request("GET", config_url, params={**API_PARAMS, "mod": 1}, headers=headers)
        )
        if (config.get("data") or {}).get("isLogin") != 1:
            raise LoginError("网关未确认登录状态")

    def _report_env(self, ticket: str, headers: dict[str, str]) -> None:
        device = uuid.uuid4().hex
        report = self._payload(
            self._request(
                "POST",
                self.ep.gateway + "/controller/v1/public/reportEnv",
                params=API_PARAMS,
                headers=headers,
                json={
                    "ticket": ticket,
                    "deviceId": device,
                    "env": {"endpoint": {"device_id": device, "device": {"type": "browser"}}},
                },
            )
        )
        if not _succeeded(report):
            raise LoginError("网关拒绝了设备环境上报")

    def _solve_captcha(self) -> str:
        """CAS 要求图形验证码时，尝试取图并本地识别。"""
        from . import captcha as ocr

        for name in ("captcha", "getCaptcha", "captchaImage"):
            try:
                response = self._request("GET", self.ep.cas + name)
            except ApiError:
                continue
            if response.status_code != 200:
                continue
            try:
                payload = response.json()
            except ValueError:
                continue
            source = _data_url(payload)
            if not source or "," not in source:
                continue
            try:
                image = base64.b64decode(source.split(",", 1)[1])
            except ValueError:
                continue
            code = ocr.recognize(image)
            if code:
                log.info(f"CAS 图形验证码识别成功：{code}")
                return code
        raise LoginError("学校要求图形验证码但自动识别失败，请先用浏览器登录一次再运行")

    def _cas_login(self, service: str) -> str:
        need = self._payload(self._request("GET", self.ep.cas + "needCaptcha"))
        captcha_code = "" if need.get("code") == 200 else self._solve_captcha()

        key_data = self._payload(self._request("POST", self.ep.cas + "getChiperKey", json={}))
        try:
            encoded = key_data["publicKey"]
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            key = load_der_public_key(raw)
            if not isinstance(key, rsa.RSAPublicKey):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise LoginError("无法解析学校下发的加密公钥") from None

        def encrypt(value: str) -> str:
            return base64.b64encode(key.encrypt(value.encode(), padding.PKCS1v15())).decode()

        data = {
            "service": service,
            "username": self.username,
            "password": encrypt(self.password),
            "captcha": captcha_code,
            "rememberMe": False,
            "loginType": "account",
            "wxBinded": False,
            "mobilePhoneNum": "",
            "fingerPrint": None,
            "mobileVerifyCode": "",
        }
        result = self._payload(self._request("POST", self.ep.cas + "casLogin", json=data))
        if result.get("code") == 200 and isinstance(result.get("redirectUrl"), str):
            return result["redirectUrl"]
        if result.get("code") == 502 or result.get("needStage2Validation"):
            raise LoginError("该账号触发了短信二次认证，无法自动完成")
        info = result.get("info") or f"code={result.get('code')}"
        raise LoginError(f"账号或密码被拒绝：{info}")

    # ── 业务接口 ────────────────────────────────────────────

    def probe(self) -> ApiProfile:
        """依次探测业务接口风格，第一个可用的即被锁定。"""
        failures: list[str] = []
        expired = False
        for profile in build_profiles(self.ep):
            try:
                rows = self._list_with(profile)
            except SessionExpired:
                expired = True
                failures.append(f"{profile.name}: 会话失效")
                continue
            except Exception as exc:
                failures.append(f"{profile.name}: {exc}")
                continue
            self.profile = profile
            log.info(f"业务接口已锁定：{profile.name}（当前 {len(rows)} 条讲座）")
            return profile
        if expired:
            raise SessionExpired("所有候选接口都返回登录页，需要重新登录")
        raise ApiError("没有可用的讲座接口：" + "；".join(failures))

    def _list_with(self, profile: ApiProfile) -> list[dict[str, Any]]:
        params = {"_": int(time.time() * 1000)}
        if profile.list_method == "GET":
            response = self._request("GET", profile.list_url, params=params)
        else:
            response = self._request("POST", profile.list_url, params={**API_PARAMS}, data={})

        if response.status_code in (401, 403):
            raise SessionExpired(f"HTTP {response.status_code}")
        if response.status_code == 404:
            raise ApiError("HTTP 404，接口路径不存在")
        if response.status_code in (301, 302):
            raise SessionExpired(f"HTTP {response.status_code} 跳转到 {_where(response.url)}")
        if response.status_code != 200:
            raise ApiError(f"HTTP {response.status_code}")

        content_type = response.headers.get("Content-Type", "")
        text = response.text
        if "html" in content_type.lower() or text.lstrip().startswith("<"):
            head = text[:3000].lower()
            if any(mark in head for mark in ("casback", "dist/main/login", self._cas_host)):
                raise SessionExpired("被跳转到 CAS 登录页")
            raise ApiError(
                f"返回 HTML 而非 JSON（HTTP {response.status_code}，{len(text)} 字节，"
                f"Content-Type={content_type or '空'}）"
            )
        rows = _extract_rows(self._payload(response))
        if rows is None:
            raise ApiError("响应是 JSON 但结构无法识别")
        return rows

    def list_lectures(self) -> list[dict[str, Any]]:
        if self.profile is None:
            self.probe()
        assert self.profile is not None
        return self._list_with(self.profile)

    def captcha(self) -> bytes | None:
        """获取验证码图片；失败返回 None。"""
        if self.profile is None:
            return None
        response = self._request(
            "GET", self.profile.vcode_url, params={"_": int(time.time() * 1000)}
        )
        if response.status_code in (401, 403):
            raise SessionExpired(f"HTTP {response.status_code}")
        if response.status_code != 200:
            return None
        try:
            payload = self._payload(response)
        except ApiError:
            return None
        source = _data_url(payload)
        if not source or "," not in source:
            return None
        try:
            return base64.b64decode(source.split(",", 1)[1])
        except ValueError:
            return None

    def submit(self, wid: str, code: str) -> str:
        """提交预约，返回语义化结果常量。"""
        if self.profile is None:
            raise ApiError("业务接口尚未探测")

        if self.profile.submit_style == "paramjson":
            data = {"paramJson": json.dumps({"HD_WID": wid, "vcode": code}, ensure_ascii=False)}
        else:
            data = {"wid": wid, "vcode": code}

        try:
            response = self._request("POST", self.profile.submit_url, data=data)
        except requests.RequestException as exc:
            log.warn(f"提交请求异常：{type(exc).__name__}")
            return FAILED

        if response.status_code in (401, 403):
            return EXPIRED
        if response.status_code == 302 and self._cas_host in response.headers.get("Location", ""):
            return EXPIRED
        if response.status_code in (429, 503):
            return BUSY
        if response.status_code != 200:
            return FAILED

        text = response.text
        if text.lstrip().startswith("<"):
            return EXPIRED
        try:
            payload = response.json()
        except ValueError:
            log.warn(f"提交响应不是 JSON：{text[:120]!r}")
            return FAILED

        if _succeeded(payload):
            return SUCCESS
        lowered = json.dumps(payload, ensure_ascii=False)
        if any(word in lowered for word in _DUP_WORDS):
            return DUPLICATE
        if any(word in lowered for word in _FULL_WORDS):
            return FULL
        if any(word in lowered for word in _NOT_OPEN_WORDS):
            return NOT_OPEN
        if any(word in lowered for word in _CAPTCHA_WORDS):
            log.info(f"提交被判定验证码错误：{lowered[:120]}")
            return CAPTCHA_WRONG
        if any(word in lowered for word in _BUSY_WORDS):
            return BUSY
        log.info(f"提交未成功：{lowered[:160]}")
        return FAILED
