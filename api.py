"""
api.py — MinerU 精准解析 API 封装。
负责：申请预签名上传 URL、curl.exe 上传、批量轮询解析结果。
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

logger = logging.getLogger(__name__)


# ── 申请上传 URL ──────────────────────────────────────────────────────────────

def apply_upload_urls(
    files: list[dict],
    token: str,
    model_version: str = "vlm",
    language: str = "ch",
    proxies: dict | None = None,
) -> tuple[str, list[str]]:
    """
    向 MinerU 申请批量预签名上传 URL。

    files: [{"name": "file.pdf", "is_ocr": False}, ...]
    返回: (batch_id, [upload_url, ...])
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "files": files,
        "model_version": model_version,
        "language": language,
    }

    resp = requests.post(
        BATCH_URL,
        json=payload,
        headers=headers,
        proxies=proxies,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("code") != 0:
        raise RuntimeError(f"申请上传 URL 失败: code={data.get('code')} msg={data.get('msg')}")

    batch_id: str = data["data"]["batch_id"]
    upload_urls: list[str] = data["data"]["file_urls"]
    logger.debug("申请到 batch_id=%s，共 %d 个上传 URL", batch_id, len(upload_urls))
    return batch_id, upload_urls


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
    token: str,
    interval: int = 8,
    timeout: int = 1800,
    proxies: dict | None = None,
    on_progress: Callable[[list[dict]], None] | None = None,
) -> list[dict]:
    """
    轮询单个批次直到所有文件完成（done / failed）或超时。

    on_progress(results) 每轮次调用一次，results 是 extract_result 列表。
    返回最终 extract_result 列表。
    状态值: waiting-file / pending / running / done / failed / converting
    """
    url = f"{RESULT_URL}/{batch_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    terminal_states = {"done", "failed"}

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.warning("轮询请求失败 batch=%s: %s，%d 秒后重试", batch_id, exc, interval)
            time.sleep(interval)
            continue

        if data.get("code") != 0:
            logger.warning("轮询返回错误 batch=%s: code=%s msg=%s",
                           batch_id, data.get("code"), data.get("msg"))
            time.sleep(interval)
            continue

        results: list[dict] = data["data"].get("extract_result", [])

        if on_progress:
            on_progress(results)

        # 检查是否全部完成
        if results and all(r.get("state") in terminal_states for r in results):
            return results

        time.sleep(interval)

    logger.error("批次 %s 轮询超时（%d 秒）", batch_id, timeout)
    return []


def poll_batches_concurrent(
    batch_ids: list[str],
    token: str,
    interval: int = 8,
    timeout: int = 1800,
    proxies: dict | None = None,
    on_batch_progress: Callable[[str, list[dict]], None] | None = None,
) -> dict[str, list[dict]]:
    """
    并发轮询多个批次，返回 {batch_id: extract_result_list}。
    on_batch_progress(batch_id, results) 每批每轮次调用。
    """
    batch_results: dict[str, list[dict]] = {}

    def _poll(bid: str) -> tuple[str, list[dict]]:
        cb = (lambda r: on_batch_progress(bid, r)) if on_batch_progress else None
        return bid, poll_batch(bid, token, interval, timeout, proxies, cb)

    # 每个批次独立线程轮询
    with ThreadPoolExecutor(max_workers=len(batch_ids) or 1) as pool:
        futures = {pool.submit(_poll, bid): bid for bid in batch_ids}
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
    token: str,
    model_version: str = "vlm",
    language: str = "ch",
    proxies: dict | None = None,
    proxy_mode: str = "system",
    proxy_url: str = "",
    on_uploaded: Callable | None = None,
) -> tuple[str, dict]:
    """
    对一批 FileNode：
    1. 申请上传 URL（含 batch_id）
    2. 并发上传所有文件
    返回 (batch_id, {FileNode: upload_success})
    """
    file_specs = [{"name": fn.path.name, "is_ocr": False} for fn in batch]
    batch_id, upload_urls = apply_upload_urls(
        file_specs, token, model_version, language, proxies
    )

    file_paths = [fn.path for fn in batch]
    upload_results = upload_batch_concurrent(
        file_paths, upload_urls, proxy_mode, proxy_url, on_uploaded
    )

    # 延迟，避免频控
    time.sleep(INTER_BATCH_DELAY)
    return batch_id, upload_results
