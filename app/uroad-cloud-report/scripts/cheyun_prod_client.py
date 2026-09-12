#!/usr/bin/env python3
"""Owned cloud pagination and streaming download client with deploy-time endpoints."""
from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

def _configured_origin(name: str, fallback: str) -> str:
    return (os.environ.get(name) or fallback).strip().rstrip("/")


def _configured_hosts(name: str, fallback: str = "") -> set[str]:
    raw = os.environ.get(name)
    source = raw if raw is not None else fallback
    return {item.strip().lower() for item in source.split(",") if item.strip()}


PRODUCTION_ORIGIN = _configured_origin("UROAD_PROD_ORIGIN", "https://vehicle-log-api.prod.example.com")
TEST_ORIGIN = _configured_origin("UROAD_TEST_ORIGIN", "https://vehicle-log-api.test.example.com")
PAGINATION_PATH = "/v1-0/msg-parsing/hu-log-files/pagination"
EEA_PLATFORM = "EEA3.0"
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
SETTINGS_PATH = Path(__file__).resolve().parents[1] / "settings.json"
CLOUD_ENVIRONMENTS = {
    "prod": {
        "origin": PRODUCTION_ORIGIN,
        "providerEnvironment": "prod",
        "displayName": "生产环境",
        "trustedDownloadHosts": {
            "https": _configured_hosts("UROAD_PROD_DOWNLOAD_HTTPS_HOSTS", "vehicle-log-download.prod.example.com"),
            "http": _configured_hosts("UROAD_PROD_DOWNLOAD_HTTP_HOSTS"),
        },
    },
    "test": {
        "origin": TEST_ORIGIN,
        "providerEnvironment": "testtwo",
        "displayName": "测试环境",
        "trustedDownloadHosts": {
            "https": _configured_hosts("UROAD_TEST_DOWNLOAD_HTTPS_HOSTS", "vehicle-log-download.test.example.com"),
            "http": _configured_hosts("UROAD_TEST_DOWNLOAD_HTTP_HOSTS"),
        },
    },
}
UROAD_LOG_CLASS = "log_fsdA_service"
CAN_LOG_CLASS = "msg_xcu"


class CloudQueryError(RuntimeError):
    """Base class for bounded cloud query failures."""


class CloudNoDataError(CloudQueryError):
    """Successful query but target data is empty or incomplete."""


class CloudConfigError(CloudQueryError):
    pass


class CloudAuthError(CloudQueryError):
    pass


class CloudNetworkError(CloudQueryError):
    pass


class CloudServerError(CloudQueryError):
    pass


class CloudResponseError(CloudQueryError):
    pass


@dataclass(frozen=True)
class QueryResult:
    items: list[dict]
    total: int
    environment: str
    provider_environment: str
    origin: str
    log_class: str
    request_host: str
    download_hosts: list[str]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_settings(path: Path | None = None) -> dict:
    settings_path = (path or SETTINGS_PATH).resolve()
    if not settings_path.is_file():
        raise CloudConfigError(
            "未找到本 Skill 的 settings.json；请复制 settings.json.example，填写 username，"
            "并将配置文件保留在本地且不要发送到聊天中。"
        )
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CloudConfigError("本 Skill 的 settings.json 无法读取") from exc
    if not isinstance(payload, dict):
        raise CloudConfigError("本 Skill 的 settings.json 必须是 JSON 对象")
    username = str(payload.get("username") or "").strip()
    if not username:
        raise CloudConfigError("本 Skill 的 settings.json 缺少非空 username")
    configured = payload.get("cloudOrigins") or {}
    if configured not in ({}, None):
        if not isinstance(configured, dict):
            raise CloudConfigError("本 Skill 的 settings.json cloudOrigins 格式无效")
        for key, value in configured.items():
            if key not in CLOUD_ENVIRONMENTS:
                raise CloudConfigError(f"settings.json 包含不受支持的环境配置: {key}")
            if str(value).strip() != CLOUD_ENVIRONMENTS[key]["origin"]:
                raise CloudConfigError(f"settings.json 中 {key} 环境地址不在白名单中")
    return {
        "username": username,
        "python_path": payload.get("python_path"),
        "last_used": payload.get("last_used"),
        "cloudOrigins": {key: value["origin"] for key, value in CLOUD_ENVIRONMENTS.items()},
    }


def normalize_cloud_env(value: str) -> str:
    env = str(value or "").strip().lower()
    if env not in CLOUD_ENVIRONMENTS:
        raise CloudConfigError(f"不支持的数据环境: {value}")
    return env


def environment_config(environment: str) -> dict:
    env = normalize_cloud_env(environment)
    config = CLOUD_ENVIRONMENTS[env]
    return {
        "environment": env,
        "origin": config["origin"],
        "providerEnvironment": config["providerEnvironment"],
        "displayName": config["displayName"],
        "trustedDownloadHosts": {
            "https": set(config["trustedDownloadHosts"]["https"]),
            "http": set(config["trustedDownloadHosts"]["http"]),
        },
    }


def provider_environment(environment: str) -> str:
    return environment_config(environment)["providerEnvironment"]


def add_download_host_allowlist(environment: str, hostname: str, scheme: str = "https") -> None:
    env = normalize_cloud_env(environment)
    host = str(hostname or "").strip().lower()
    protocol = str(scheme or "").strip().lower()
    if protocol not in {"http", "https"} or not host:
        raise CloudConfigError("下载 host 白名单参数无效")
    CLOUD_ENVIRONMENTS[env]["trustedDownloadHosts"][protocol].add(host)


def trusted_download_hosts(environment: str, scheme: str = "https") -> set[str]:
    config = environment_config(environment)
    protocol = str(scheme or "").strip().lower()
    if protocol not in {"http", "https"}:
        raise CloudConfigError("下载协议无效")
    return set(config["trustedDownloadHosts"][protocol])


def _safe_service_error(message: object, username: str = "") -> str:
    text = str(message or "unknown error").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"https?://[^\s<>\"']+", "[URL]", text, flags=re.IGNORECASE)
    if username:
        text = text.replace(username, "[USERNAME]")
    return text[:240]


def _read_bounded_json(response) -> dict:
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > MAX_RESPONSE_BYTES:
                raise CloudResponseError("查询响应超过大小限制")
        except ValueError as exc:
            raise CloudResponseError("查询响应长度无效") from exc
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CloudResponseError("查询响应超过大小限制")
    if not raw:
        raise CloudResponseError("查询返回为空")
    return json.loads(raw.decode("utf-8"))


def _query_url(origin: str, params: dict) -> str:
    return origin + PAGINATION_PATH + "?" + urllib.parse.urlencode(params)


def request_json(params: dict, username: str, *, environment: str) -> dict:
    config = environment_config(environment)
    url = _query_url(config["origin"], params)
    parsed = urllib.parse.urlsplit(url)
    expected = urllib.parse.urlsplit(config["origin"])
    if parsed.scheme != "https" or parsed.netloc != expected.netloc or parsed.path != PAGINATION_PATH:
        raise CloudConfigError("列表查询必须使用固定环境端点和路径")
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "content-type": "application/json", "oper-path": "msg-search"},
        method="GET",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=60) as response:
            payload = _read_bounded_json(response)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        if 300 <= code < 400:
            raise CloudResponseError(f"{environment} 查询拒绝 HTTP 重定向 {code}") from exc
        if code in {401, 403}:
            raise CloudAuthError(f"{environment} 查询认证失败 HTTP {code}") from exc
        if 500 <= code < 600:
            raise CloudServerError(f"{environment} 查询服务失败 HTTP {code}") from exc
        raise CloudResponseError(f"{environment} 查询失败 HTTP {code}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, socket.timeout):
            raise CloudNetworkError(f"{environment} 查询网络超时") from exc
        raise CloudNetworkError(f"{environment} 查询网络连接失败") from exc
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CloudResponseError(f"{environment} 查询返回了无效 JSON") from exc
    if not isinstance(payload, dict):
        raise CloudResponseError(f"{environment} 查询返回格式无效")
    if payload.get("success") is False:
        raise CloudResponseError(f"{environment} 查询失败: " + _safe_service_error(payload.get("message"), username))
    return payload


def build_query_params(*, vin: str, start: str, end: str, log_class: str, username: str,
                       page_no: int = 1, page_size: int = 100, eea_platform: str = EEA_PLATFORM) -> dict:
    if not vin or not log_class or page_no <= 0 or page_size <= 0:
        raise CloudConfigError("查询参数无效")
    if eea_platform != EEA_PLATFORM:
        raise CloudConfigError(f"EEA 平台必须是 {EEA_PLATFORM}")
    params = {
        "vin": vin,
        "logClass": log_class,
        "collectTimeMin": start,
        "collectTimeMax": end,
        "remoteMode": "false",
        "pageNo": page_no,
        "pageSize": page_size,
        "userName": username,
        "eeaPlatform": EEA_PLATFORM,
    }
    if log_class == UROAD_LOG_CLASS:
        params.update({
            "logLevel": "",
            "categoryCode": "",
            "categoryId": "",
            "keywordCode": "",
            "domain": "",
        })
    elif log_class == CAN_LOG_CLASS:
        params.update({"domain": ""})
    else:
        raise CloudConfigError(f"不支持的 logClass: {log_class}")
    return params


def extract_download_hosts(items: list[dict]) -> list[str]:
    hosts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for field in ("downloadURL", "downloadUrl", "url"):
            value = item.get(field)
            if not value:
                continue
            parsed = urllib.parse.urlsplit(str(value))
            host = (parsed.hostname or "").strip().lower()
            if host:
                hosts.append(f"{parsed.scheme.lower() or 'unknown'}://{host}")
            break
    return sorted(dict.fromkeys(hosts))


def query_files(*, vin: str, start: str, end: str, log_class: str, username: str,
                page_size: int = 100, eea_platform: str = EEA_PLATFORM, environment: str = "prod") -> QueryResult:
    env = normalize_cloud_env(environment)
    all_items: list[dict] = []
    page_no = 1
    expected_total: int | None = None
    seen_pages: set[str] = set()
    seen_records: set[str] = set()
    while True:
        params = build_query_params(
            vin=vin, start=start, end=end, log_class=log_class, username=username,
            page_no=page_no, page_size=page_size, eea_platform=eea_platform,
        )
        payload = request_json(params, username, environment=env)
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise CloudResponseError(f"{env} 查询 data 格式无效")
        items = data.get("list") or data.get("records") or []
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise CloudResponseError(f"{env} 查询文件列表格式无效")
        total = int(data.get("total", len(all_items) + len(items)))
        if total < 0:
            raise CloudResponseError(f"{env} 查询 total 无效")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise CloudResponseError(f"{env} 查询 total 在分页期间不一致")
        if len(all_items) + len(items) > total:
            raise CloudResponseError(f"{env} 查询记录数超过 total")
        page_fingerprint = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if page_fingerprint in seen_pages:
            raise CloudResponseError(f"{env} 查询分页重复且未推进")
        seen_pages.add(page_fingerprint)
        record_keys = {
            json.dumps((item.get("fileKey"), item.get("fileName") or item.get("name"),
                        item.get("collectTime"), item.get("downloadURL") or item.get("downloadUrl") or item.get("url")),
                       ensure_ascii=False, separators=(",", ":"))
            for item in items
        }
        if items and record_keys and record_keys.issubset(seen_records):
            raise CloudResponseError(f"{env} 查询分页没有新增记录")
        seen_records.update(record_keys)
        if not items and len(all_items) < total:
            raise CloudResponseError(f"{env} 查询在达到 total 前返回空页")
        all_items.extend(items)
        if not items or len(all_items) >= total:
            config = environment_config(env)
            return QueryResult(
                items=all_items,
                total=total,
                environment=env,
                provider_environment=config["providerEnvironment"],
                origin=config["origin"],
                log_class=log_class,
                request_host=urllib.parse.urlsplit(config["origin"]).hostname or "",
                download_hosts=extract_download_hosts(all_items),
            )
        page_no += 1


def safe_filename(item: dict, index: int = 0) -> str:
    value = str(item.get("fileName") or item.get("name") or f"source-{index}.zst")
    name = Path(value.replace("\\", "/")).name
    if name in {"", ".", ".."}:
        raise CloudResponseError("文件记录缺少有效文件名")
    return name


def validate_download_url(url: str, *, environment: str) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(str(url))
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    trusted_https = trusted_download_hosts(environment, "https")
    trusted_http = trusted_download_hosts(environment, "http")
    if scheme == "https" and host in trusted_https and parsed.netloc and not parsed.username and not parsed.password:
        return scheme, host
    if scheme == "http" and host in trusted_http and parsed.netloc and not parsed.username and not parsed.password and parsed.port in {None, 80}:
        return scheme, host
    raise CloudResponseError(f"{environment} 文件下载地址不在白名单中")


def download_to_file(url: str, destination: Path, *, environment: str, max_bytes: int = MAX_DOWNLOAD_BYTES) -> int:
    scheme, host = validate_download_url(url, environment=environment)
    parsed = urllib.parse.urlsplit(str(url))
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")
    part.unlink(missing_ok=True)
    request = urllib.request.Request(str(url), method="GET")
    opener = urllib.request.build_opener(_NoRedirect())
    total = 0
    try:
        with opener.open(request, timeout=180) as response, part.open("xb") as output:
            resolved = urllib.parse.urlsplit(response.geturl())
            if (resolved.scheme.lower(), resolved.hostname, resolved.port) != (scheme, host, parsed.port):
                raise CloudResponseError(f"{environment} 文件下载拒绝跨主机重定向")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > max_bytes:
                raise CloudResponseError(f"{environment} 文件超过单文件大小限制")
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise CloudResponseError(f"{environment} 文件超过单文件大小限制")
                output.write(chunk)
            if total == 0:
                raise CloudResponseError(f"{environment} 文件下载为空")
            output.flush()
            os.fsync(output.fileno())
        part.replace(destination)
        return total
    except urllib.error.HTTPError as exc:
        exc.close()
        if 300 <= exc.code < 400:
            raise CloudResponseError(f"{environment} 文件下载拒绝 HTTP 重定向 {exc.code}") from exc
        if exc.code in {401, 403}:
            raise CloudAuthError(f"{environment} 文件下载认证失败 HTTP {exc.code}") from exc
        if 500 <= exc.code < 600:
            raise CloudServerError(f"{environment} 文件下载服务失败 HTTP {exc.code}") from exc
        raise CloudResponseError(f"{environment} 文件下载失败 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, socket.timeout):
            raise CloudNetworkError(f"{environment} 文件下载网络超时") from exc
        raise CloudNetworkError(f"{environment} 文件下载网络连接失败") from exc
    finally:
        part.unlink(missing_ok=True)


def ordered_files(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda item: (
        str(item.get("collectTime") or ""), str(item.get("fileName") or item.get("name") or ""),
        str(item.get("fileKey") or ""),
    ))
