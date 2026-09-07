"""快麦开放平台只读客户端：官方参数、签名、HTTP、分页响应和会话续期。

签名与公共参数依据官方公开全文「API调用方法详解」（llms-full.txt，快照
SHA-256 见 docs/superpowers/research/2026-09-06-kuaimai-data-verification.md）。
错误只携带脱敏类别，不携带服务端原文；日志只输出字段白名单。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel

BEIJING = ZoneInfo("Asia/Shanghai")
ROUTER_URL = "https://gw.superboss.cc/router"
VERSION = "1.0"
SIGN_METHOD = "hmac-sha256"
MAX_ATTEMPTS = 3
RETRY_AFTER_CAP_SECONDS = 5.0

logger = logging.getLogger(__name__)

_sleep = time.sleep


class KuaimaiError(Exception):
    """脱敏类别错误：authentication/permission/rate_limit/timeout/upstream/invalid_response/unknown_empty。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class Page(BaseModel):
    rows: list[dict[str, Any]]
    total: int | None
    has_next: bool | None
    cursor: str | None
    verified_empty: bool


def sign(params: Mapping[str, str], secret: str) -> str:
    """官方签名：键名升序拼接 k+v（排除 sign），HMAC-SHA256 后转大写十六进制。"""
    canonical = "".join(k + params[k] for k in sorted(params) if k != "sign")
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest().upper()


def parse_page(payload: dict[str, object], *, allow_omitted_list: bool = False) -> Page:
    """校验分页响应形状；省略列表仅能由已实测接口显式解释为空页。"""
    if payload.get("success") is False:
        raise KuaimaiError("upstream")
    rows = payload.get("list")
    total = payload.get("total")
    has_next_raw = payload.get("hasNext")
    has_next = has_next_raw if isinstance(has_next_raw, bool) else None
    list_omitted = rows is None
    if rows is None:
        if total == 0 or (total is None and (has_next is False or allow_omitted_list)):
            rows = []
        else:
            raise KuaimaiError("unknown_empty" if total is None else "invalid_response")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise KuaimaiError("invalid_response")
    cursor_raw = payload.get("cursor")
    cursor = cursor_raw if isinstance(cursor_raw, str) else None
    verified_empty = len(rows) == 0 and (
        total == 0 or has_next is False
        or (allow_omitted_list and list_omitted and total is None)
    )
    return Page(
        rows=rows,  # type: ignore[arg-type]
        total=total if isinstance(total, int) else None,
        has_next=has_next,
        cursor=cursor,
        verified_empty=verified_empty,
    )


def _classify_error_code(code: object, message: object) -> str:
    text = f"{code} {message}".lower()
    if "token" in text or "session" in text or "授权" in str(message) or "签名" in str(message):
        return "authentication"
    if "权限" in str(message):
        return "permission"
    if "频繁" in str(message) or "限流" in str(message) or "rate" in text:
        return "rate_limit"
    return "upstream"


class KuaimaiClient:
    """一次调用一层：公共参数 + 签名 + 表单 POST + 分页形状校验。"""

    def __init__(self, settings, http: httpx.Client):
        self.settings = settings
        self.http = http

    def call(self, method: str, params: dict[str, str]) -> dict[str, object]:
        secret = self.settings.app_secret.get_secret_value()
        last_error: KuaimaiError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            public: dict[str, str] = {
                "appKey": self.settings.app_key.get_secret_value(),
                "session": self.settings.access_token.get_secret_value(),
                "method": method,
                "timestamp": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S"),
                "version": VERSION,
                "sign_method": SIGN_METHOD,
            }
            public.update(params)
            public["sign"] = sign(public, secret)
            try:
                response = self.http.post(ROUTER_URL, data=public, follow_redirects=False)
            except httpx.TransportError as exc:
                logger.warning("kuaimai transport error attempt=%s", attempt)
                last_error = KuaimaiError("timeout" if isinstance(exc, httpx.TimeoutException) else "upstream")
                if attempt < MAX_ATTEMPTS:
                    _sleep(2.0 ** (attempt - 1))
                continue
            if response.status_code == 429:
                logger.warning("kuaimai rate limit attempt=%s", attempt)
                retry_after = response.headers.get("Retry-After")
                delay = RETRY_AFTER_CAP_SECONDS
                if retry_after is not None and retry_after.isdigit():
                    delay = min(float(retry_after), RETRY_AFTER_CAP_SECONDS)
                last_error = KuaimaiError("rate_limit")
                if attempt < MAX_ATTEMPTS:
                    _sleep(delay)
                continue
            if response.status_code in (401, 403):
                raise KuaimaiError("authentication")
            if response.status_code >= 500:
                logger.warning("kuaimai server error attempt=%s", attempt)
                last_error = KuaimaiError("upstream")
                if attempt < MAX_ATTEMPTS:
                    _sleep(2.0 ** (attempt - 1))
                continue
            if response.status_code != 200:
                raise KuaimaiError("upstream")
            try:
                payload = response.json()
            except (json.JSONDecodeError, ValueError):
                raise KuaimaiError("invalid_response") from None
            if not isinstance(payload, dict):
                raise KuaimaiError("invalid_response")
            if payload.get("success") is False:
                code = _classify_error_code(payload.get("code"), payload.get("message"))
                if code in ("rate_limit",) and attempt < MAX_ATTEMPTS:
                    last_error = KuaimaiError(code)
                    _sleep(2.0 ** (attempt - 1))
                    continue
                raise KuaimaiError(code)
            return payload
        raise last_error or KuaimaiError("upstream")

    def refresh_session(self, *, now: datetime | None = None) -> datetime:
        """续期会话：核对两个 Token 不变，返回已核验单位后的过期时刻。

        普通查询不自动刷新；返回 token 意外变化时停止自动处理。
        """
        payload = self.call("open.token.refresh", {
            "refreshToken": self.settings.refresh_token.get_secret_value(),
        })
        new_token = payload.get("accessToken") or payload.get("sessionKey")
        if isinstance(new_token, str) and new_token != self.settings.access_token.get_secret_value():
            raise KuaimaiError("upstream")
        expire_raw = payload.get("expireTime")
        if not isinstance(expire_raw, str) or not expire_raw.strip():
            raise KuaimaiError("invalid_response")
        try:
            expires = datetime.strptime(expire_raw.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING)
        except ValueError:
            raise KuaimaiError("invalid_response") from None
        reference = now or datetime.now(BEIJING)
        if expires <= reference:
            raise KuaimaiError("invalid_response")
        return expires
