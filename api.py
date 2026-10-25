"""
api.py — MinerU 精准解析 API 封装。
负责：申请预签名上传 URL、curl.exe 上传、批量轮询解析结果。

v2：接入 errors.py（统一错误处理）和 TokenManager（负载均衡 + 自动切换）。
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import requests

from config import (
    BATCH_URL,
    RESULT_URL,
    MAX_UPLOAD_WORKERS,
    INTER_BATCH_DELAY,
    HTTP_TIMEOUT,
    SUBMIT_MIN_INTERVAL,
    SUBMIT_MAX_RETRIES,
    SUBMIT_BACKOFF_BASE,
    SUBMIT_MAX_BACKOFF,
    build_curl_proxy_args,
)
from errors import ErrorCategory, MinerUApiError, NoAvailableTokenError, parse_api_response
from token_manager import TokenManager

logger = logging.getLogger(__name__)

_SUBMIT_LOCK = threading.Lock()
_LAST_SUBMIT_TS = 0.0


# ── 申请上传 URL ──────────────────────────────────────────────────────────────

def _retry_after_seconds(resp: requests.Response) -> float | None:
    raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(seconds, SUBMIT_MAX_BACKOFF))


def _backoff_seconds(attempt: int) -> float:
    exponent = max(attempt - 1, 0)
    return min(SUBMIT_BACKOFF_BASE * (2 ** exponent), SUBMIT_MAX_BACKOFF)


def _wait_before_submit() -> None:
    global _LAST_SUBMIT_TS
    wait = SUBMIT_MIN_INTERVAL - (time.monotonic() - _LAST_SUBMIT_TS)
    if wait > 0:
        time.sleep(wait)
    _LAST_SUBMIT_TS = time.monotonic()


def apply_upload_urls(
    files: list[dict],
    token_manager: TokenManager,
    model_version: str = "vlm",
    language: str = "ch",
    proxies: dict | None = None,
) -> tuple[str, list[str], int]:
    """
    向 MinerU 申请批量预签名上传 URL，自动选择 Token，失败时切换重试。

    files: [{"name": "file.pdf", "is_ocr": False}, ...]
    返回: (batch_id, [upload_url, ...], token_idx)

    同一进程内的申请请求串行化，并对 429 / 网络错误 / 暂时性业务错误做指数退避。
    """
    payload = {
        "files": files,
        "model_version": model_version,
        "language": language,
    }

    last_exc: Exception | None = None
    attempts = 0

    with _SUBMIT_LOCK:
        while True:
            try:
                token_idx, token = token_manager.get_token()
            except NoAvailableTokenError as exc:
                message = "所有 Token 均不可用，无法申请上传 URL"
                if last_exc:
                    message += f"，最后错误: {last_exc}"
                raise RuntimeError(message) from exc

            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            logger.debug("[apply_upload_urls] 尝试 Token[%d] token_prefix=%s... 文件数=%d",
                         token_idx, token[:8] if len(token) > 8 else token, len(files))
            _wait_before_submit()

            try:
                resp = requests.post(
                    BATCH_URL,
                    json=payload,
                    headers=headers,
                    proxies=proxies,
                    timeout=HTTP_TIMEOUT,
                )
            except requests.RequestException as exc:
                last_exc = exc
                attempts += 1
                if attempts > SUBMIT_MAX_RETRIES:
                    raise RuntimeError(
                        f"申请上传 URL 网络错误，已重试 {SUBMIT_MAX_RETRIES} 次: {exc}"
                    ) from exc
                wait = _backoff_seconds(attempts)
                logger.warning("申请上传 URL 网络错误: %s，%.0f 秒后重试（%d/%d）",
                               exc, wait, attempts, SUBMIT_MAX_RETRIES)
                time.sleep(wait)
                continue

            logger.debug("[apply_upload_urls] Token[%d] HTTP %d", token_idx, resp.status_code)

            if resp.status_code == 401:
                token_manager.report_error(token_idx, ErrorCategory.AUTH)
                last_exc = RuntimeError(f"Token[{token_idx}] 认证失败 (401)")
                logger.warning("Token[%d] 认证失败 (401)，切换 Token 重试", token_idx)
                continue

            if resp.status_code == 429:
                last_exc = RuntimeError(f"Token[{token_idx}] 限流 (429)")
                if token_manager.has_other_available(token_idx):
                    token_manager.report_error(token_idx, ErrorCategory.RATE_LIMIT)
                    logger.warning("Token[%d] 限流 (429)，切换 Token 重试", token_idx)
                    continue
                attempts += 1
                if attempts > SUBMIT_MAX_RETRIES:
                    token_manager.report_error(token_idx, ErrorCategory.RATE_LIMIT)
                    raise RuntimeError(
                        f"申请上传 URL 持续被限流，已重试 {SUBMIT_MAX_RETRIES} 次: {last_exc}"
                    ) from last_exc
                retry_after = _retry_after_seconds(resp)
                wait = retry_after if retry_after is not None else _backoff_seconds(attempts)
                logger.warning("Token[%d] 限流 (429)，%.0f 秒后重试（%d/%d）",
                               token_idx, wait, attempts, SUBMIT_MAX_RETRIES)
                time.sleep(wait)
                continue

            if resp.status_code >= 500:
                last_exc = RuntimeError(f"申请上传 URL 服务错误: HTTP {resp.status_code}")
                attempts += 1
                if attempts > SUBMIT_MAX_RETRIES:
                    raise RuntimeError(
                        f"申请上传 URL 服务错误，已重试 {SUBMIT_MAX_RETRIES} 次: "
                        f"HTTP {resp.status_code}"
                    ) from last_exc
                wait = _backoff_seconds(attempts)
                logger.warning("申请上传 URL HTTP %d，%.0f 秒后重试（%d/%d）",
                               resp.status_code, wait, attempts, SUBMIT_MAX_RETRIES)
                time.sleep(wait)
                continue

            if resp.status_code >= 400:
                raise RuntimeError(
                    f"申请上传 URL 失败: HTTP {resp.status_code} {resp.text[:200]}"
                )

            try:
                data = resp.json()
                parse_api_response(data)
            except MinerUApiError as exc:
                last_exc = exc
                category = exc.info.category
                if category in (ErrorCategory.AUTH, ErrorCategory.RATE_LIMIT):
                    token_manager.report_error(token_idx, category)
                    logger.warning("Token[%d] 申请上传失败（%s），切换 Token 重试",
                                   token_idx, category.value)
                    continue
                if category == ErrorCategory.TRANSIENT:
                    attempts += 1
                    if attempts > SUBMIT_MAX_RETRIES:
                        raise RuntimeError(
                            f"申请上传 URL 持续失败，已重试 {SUBMIT_MAX_RETRIES} 次: {exc}"
                        ) from exc
                    wait = _backoff_seconds(attempts)
                    logger.warning("申请上传 URL 暂时失败: %s，%.0f 秒后重试（%d/%d）",
                                   exc, wait, attempts, SUBMIT_MAX_RETRIES)
                    time.sleep(wait)
                    continue
                raise
            except ValueError as exc:
                last_exc = exc
                attempts += 1
                if attempts > SUBMIT_MAX_RETRIES:
                    raise RuntimeError(f"申请上传 URL 响应解析失败: {exc}") from exc
                wait = _backoff_seconds(attempts)
                logger.warning("申请上传 URL 响应解析失败: %s，%.0f 秒后重试", exc, wait)
                time.sleep(wait)
                continue

            batch_id: str = data["data"]["batch_id"]
            upload_urls: list[str] = data["data"]["file_urls"]
            logger.debug("[apply_upload_urls] 成功 batch_id=%s url_count=%d Token[%d]",
                         batch_id, len(upload_urls), token_idx)
            return batch_id, upload_urls, token_idx


# ── curl.exe 上传 ─────────────────────────────────────────────────────────────

def upload_file_curl(
    local_path: Path,
    upload_url: str,
    proxy_mode: str = "system",
    proxy_url: str = "",
) -> bool:
    """
    用 curl.exe 将本地文件上传到 OSS 预签名 URL。
    规避 Python requests 在 Windows 上对阿里云 OSS 的 SSLEOFError。
    返回 True 表示成功。
    """
    proxy_args = build_curl_proxy_args(proxy_mode, proxy_url)
    cmd = ["curl.exe", "-s", "-S", "-X", "PUT", "-T", str(local_path)] + proxy_args + [upload_url]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logger.warning("curl 上传失败 %s: returncode=%d stderr=%s",
                           local_path.name, result.returncode, result.stderr[:200])
            return False
        # OSS 成功返回空 body，失败会有 XML 错误信息
        if result.stdout and "<Error>" in result.stdout:
            logger.warning("curl 上传 OSS 错误 %s: %s", local_path.name, result.stdout[:300])
            return False
        logger.debug("上传成功: %s", local_path.name)
        return True
    except subprocess.TimeoutExpired:
        logger.warning("curl 上传超时: %s", local_path.name)
        return False
    except FileNotFoundError:
        raise RuntimeError(
            "未找到 curl.exe。请确认已安装 curl（Windows 10+ 内置）或将其加入 PATH。"
        )


def upload_batch_concurrent(
    file_paths: list[Path],
    upload_urls: list[str],
    proxy_mode: str = "system",
    proxy_url: str = "",
    on_uploaded: Callable[[Path, bool], None] | None = None,
) -> dict[Path, bool]:
    """
    并发上传一批文件。返回 {path: 是否成功} 字典。
    on_uploaded(path, success) 回调用于进度更新。
    """
    results: dict[Path, bool] = {}

    def _upload(path: Path, url: str) -> tuple[Path, bool]:
        ok = upload_file_curl(path, url, proxy_mode, proxy_url)
        return path, ok

    with ThreadPoolExecutor(max_workers=MAX_UPLOAD_WORKERS) as pool:
        futures = {
            pool.submit(_upload, p, u): p
            for p, u in zip(file_paths, upload_urls)
        }
        for future in as_completed(futures):
            path, ok = future.result()
            results[path] = ok
            if on_uploaded:
                on_uploaded(path, ok)

    return results


# ── 轮询解析结果 ──────────────────────────────────────────────────────────────

def poll_batch(
    batch_id: str,
    token_idx: int,
    token_manager: TokenManager,
    interval: int = 8,
    timeout: int = 1800,
    proxies: dict | None = None,
    on_progress: Callable[[list[dict]], None] | None = None,
) -> list[dict]:
    """
    轮询单个批次直到所有文件完成（done / failed）或超时。
    若轮询时 Token 失效（AUTH/RATE_LIMIT），自动切换到其他可用 Token。

    on_progress(results) 每轮次调用一次。
    返回最终 extract_result 列表。
    """
    url = f"{RESULT_URL}/{batch_id}"
    terminal_states = {"done", "failed"}
    deadline = time.monotonic() + timeout
    current_idx = token_idx

    while time.monotonic() < deadline:
        token = token_manager.entries[current_idx].token
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("轮询请求失败 batch=%s: %s，%d 秒后重试", batch_id, exc, interval)
            time.sleep(interval)
            continue

        # HTTP 401 / 429 → 切换 Token
        if resp.status_code in (401, 429):
            category = ErrorCategory.AUTH if resp.status_code == 401 else ErrorCategory.RATE_LIMIT
            token_manager.report_error(current_idx, category)
            logger.warning("轮询 Token[%d] HTTP %d，切换 Token", current_idx, resp.status_code)
            try:
                current_idx, token = token_manager.get_token()
            except NoAvailableTokenError:
                logger.error("所有 Token 均不可用，轮询终止 batch=%s", batch_id)
                return []
            time.sleep(interval)
            continue

        try:
            resp.raise_for_status()
            data = resp.json()
            parse_api_response(data)
        except MinerUApiError as exc:
            if exc.info.category in (ErrorCategory.AUTH, ErrorCategory.RATE_LIMIT):
                token_manager.report_error(current_idx, exc.info.category)
                logger.warning("轮询 Token[%d] 失效（%s），切换 Token",
                               current_idx, exc.info.category.value)
                try:
                    current_idx, token = token_manager.get_token()
                except NoAvailableTokenError:
                    logger.error("所有 Token 均不可用，轮询终止 batch=%s", batch_id)
                    return []
            else:
                logger.warning("轮询返回错误 batch=%s: %s，%d 秒后重试", batch_id, exc, interval)
            time.sleep(interval)
            continue
        except requests.RequestException as exc:
            logger.warning("轮询请求失败 batch=%s: %s，%d 秒后重试", batch_id, exc, interval)
            time.sleep(interval)
            continue

        results: list[dict] = data["data"].get("extract_result", [])

        if on_progress:
            on_progress(results)

        if results and all(r.get("state") in terminal_states for r in results):
            return results

        time.sleep(interval)

    logger.error("批次 %s 轮询超时（%d 秒）", batch_id, timeout)
    return []


def poll_batches_concurrent(
    batch_token_pairs: list[tuple[str, int]],
    token_manager: TokenManager,
    interval: int = 8,
    timeout: int = 1800,
    proxies: dict | None = None,
    on_batch_progress: Callable[[str, list[dict]], None] | None = None,
) -> dict[str, list[dict]]:
    """
    并发轮询多个批次，返回 {batch_id: extract_result_list}。
    batch_token_pairs: [(batch_id, token_idx), ...]
    on_batch_progress(batch_id, results) 每批每轮次调用。
    """
    batch_results: dict[str, list[dict]] = {}

    def _poll(bid: str, tidx: int) -> tuple[str, list[dict]]:
        cb = (lambda r: on_batch_progress(bid, r)) if on_batch_progress else None
        return bid, poll_batch(bid, tidx, token_manager, interval, timeout, proxies, cb)

    with ThreadPoolExecutor(max_workers=max(len(batch_token_pairs), 1)) as pool:
        futures = {pool.submit(_poll, bid, tidx): bid for bid, tidx in batch_token_pairs}
        for future in as_completed(futures):
            bid, results = future.result()
            batch_results[bid] = results

    return batch_results


# ── 批次构建工具 ───────────────────────────────────────────────────────────────

def build_batches(file_nodes: list, batch_size: int) -> list[list]:
    """将 FileNode 列表按 batch_size 分批，返回批次列表。"""
    return [file_nodes[i:i + batch_size] for i in range(0, len(file_nodes), batch_size)]


def submit_and_upload_batch(
    batch: list,          # list[FileNode]
    token_manager: TokenManager,
    model_version: str = "vlm",
    language: str = "ch",
    proxies: dict | None = None,
    proxy_mode: str = "system",
    proxy_url: str = "",
    on_uploaded: Callable | None = None,
) -> tuple[str, dict, int]:
    """
    对一批 FileNode：
    1. 申请上传 URL（含 batch_id），自动选择 Token
    2. 并发上传所有文件
    返回 (batch_id, {Path: upload_success}, token_idx)
    """
    file_specs = [{"name": fn.path.name, "is_ocr": False} for fn in batch]
    batch_id, upload_urls, token_idx = apply_upload_urls(
        file_specs, token_manager, model_version, language, proxies
    )

    file_paths = [fn.path for fn in batch]
    upload_results = upload_batch_concurrent(
        file_paths, upload_urls, proxy_mode, proxy_url, on_uploaded
    )

    time.sleep(INTER_BATCH_DELAY)
    return batch_id, upload_results, token_idx
