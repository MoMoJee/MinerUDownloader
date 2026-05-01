"""
api.py — MinerU 精准解析 API 封装。
负责：申请预签名上传 URL、curl.exe 上传、批量轮询解析结果。

v2：接入 errors.py（统一错误处理）和 TokenManager（负载均衡 + 自动切换）。
"""
from __future__ import annotations

import logging
import subprocess
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
    build_curl_proxy_args,
)
from errors import ErrorCategory, MinerUApiError, NoAvailableTokenError, parse_api_response
from token_manager import TokenManager

logger = logging.getLogger(__name__)


# ── 申请上传 URL ──────────────────────────────────────────────────────────────

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
    """
    payload = {
        "files": files,
        "model_version": model_version,
        "language": language,
    }

    last_exc: Exception | None = None
    tried: set[int] = set()

    while True:
        try:
            token_idx, token = token_manager.get_token()
        except NoAvailableTokenError as exc:
            raise RuntimeError("所有 Token 均不可用，无法申请上传 URL") from exc

        if token_idx in tried:
            # 已全部尝试一圈
            raise RuntimeError(f"所有 Token 均申请上传 URL 失败，最后错误: {last_exc}")
        tried.add(token_idx)

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                BATCH_URL,
                json=payload,
                headers=headers,
                proxies=proxies,
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"申请上传 URL 网络错误: {exc}") from exc

        # HTTP 401 / 429 → 标记 Token 并切换重试（不当作致命网络错误）
        if resp.status_code == 401:
            token_manager.report_error(token_idx, ErrorCategory.AUTH)
            last_exc = RuntimeError(f"Token[{token_idx}] 认证失败 (401)")
            logger.warning("Token[%d] 认证失败 (401)，切换 Token 重试", token_idx)
            continue
        if resp.status_code == 429:
            token_manager.report_error(token_idx, ErrorCategory.RATE_LIMIT)
            last_exc = RuntimeError(f"Token[{token_idx}] 限速 (429)")
            logger.warning("Token[%d] 限速 (429)，切换 Token 重试", token_idx)
            continue

        try:
            resp.raise_for_status()
            data = resp.json()
            parse_api_response(data)
        except MinerUApiError as exc:
            last_exc = exc
            if exc.info.category in (ErrorCategory.AUTH, ErrorCategory.RATE_LIMIT):
                token_manager.report_error(token_idx, exc.info.category)
                logger.warning("Token[%d] 申请上传失败（%s），切换 Token 重试",
                               token_idx, exc.info.category.value)
                continue
            raise
        except requests.RequestException as exc:
            raise RuntimeError(f"申请上传 URL 网络错误: {exc}") from exc

        # used_batches 已在 get_token() 内部递增，此处不重复计数
        batch_id: str = data["data"]["batch_id"]
        upload_urls: list[str] = data["data"]["file_urls"]
        logger.debug("申请到 batch_id=%s，共 %d 个上传 URL（Token[%d]）",
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
