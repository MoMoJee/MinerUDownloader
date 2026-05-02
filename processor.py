"""
processor.py — ZIP 下载、解压、文件整理。

解压规则：
  full.md          → {output_stem}.md，放在 file_path.parent
  images/*         → file_path.parent / images /（UUID 命名，无冲突）
  *.json           → 仅 keep_json=True 时保留
  *_origin.pdf     → 始终丢弃
  其余              → 丢弃
"""
from __future__ import annotations

import io
import logging
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import requests

from config import MAX_DOWNLOAD_WORKERS, DOWNLOAD_RETRY, HTTP_TIMEOUT

logger = logging.getLogger(__name__)


# ── 核心解压函数 ───────────────────────────────────────────────────────────────

def extract_result(
    zip_url: str,
    file_path: Path,
    keep_zip: bool,
    keep_json: bool,
    output_stem: str,
    proxies: dict | None = None,
) -> Path:
    """
    下载并解压单个文件的解析结果 ZIP。

    参数：
      zip_url     : 解析结果 ZIP 的下载地址
      file_path   : 原始文档的绝对路径（用于确定目标目录）
      keep_zip    : 是否保留原始 ZIP 文件
      keep_json   : 是否保留 JSON 中间文件
      output_stem : 输出文件名主干（RENAME 模式时为 stem_1 等）
      proxies     : requests 代理字典

    返回：生成的 .md 文件路径
    """
    out_dir = file_path.parent
    images_dir = out_dir / "images"

    # 1. 下载 ZIP（streaming，避免大文件内存溢出）
    zip_bytes = _download_with_retry(zip_url, proxies)

    # 2. 可选保存 ZIP
    if keep_zip:
        zip_save_path = out_dir / f"{output_stem}.zip"
        zip_save_path.write_bytes(zip_bytes)
        logger.debug("已保存 ZIP: %s", zip_save_path)

    # 3. 解压
    md_content: bytes | None = None
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            p = Path(name)
            fname = p.name
            parts = p.parts

            # full.md
            if fname == "full.md":
                md_content = zf.read(name)
                continue

            # images/*
            if len(parts) >= 2 and parts[0] == "images":
                images_dir.mkdir(parents=True, exist_ok=True)
                dest = images_dir / fname
                dest.write_bytes(zf.read(name))
                continue

            # *_origin.pdf → 丢弃
            if fname.endswith("_origin.pdf"):
                continue

            # *.json → 按需保留
            if fname.endswith(".json") and keep_json:
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / fname).write_bytes(zf.read(name))
                continue

            # 其余丢弃

    if md_content is None:
        raise RuntimeError(f"ZIP 中未找到 full.md: {zip_url}")

    # 4. 写入 .md（full.md 中图片引用为 images/xxx，与共享 images/ 同级，无需修改路径）
    md_path = out_dir / f"{output_stem}.md"
    md_path.write_bytes(md_content)
    logger.debug("已写入 %s", md_path)
    return md_path


# ── 下载工具 ──────────────────────────────────────────────────────────────────

def _download_with_retry(
    url: str,
    proxies: dict | None,
    retries: int = DOWNLOAD_RETRY,
) -> bytes:
    """带指数退避重试的 HTTP GET 下载，返回 bytes。"""
    last_exc: Exception | None = None
    for attempt in range(retries):
        logger.debug("[download] attempt=%d/%d url=%s", attempt + 1, retries, url[:80])
        try:
            resp = requests.get(url, proxies=proxies, timeout=HTTP_TIMEOUT, stream=True)
            resp.raise_for_status()
            chunks = []
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    chunks.append(chunk)
            return b"".join(chunks)
        except requests.RequestException as exc:
            last_exc = exc
            wait = 2 ** attempt
            logger.warning("下载失败 (attempt %d/%d): %s，%ds 后重试", attempt + 1, retries, exc, wait)
            time.sleep(wait)

    raise RuntimeError(f"下载失败，已重试 {retries} 次: {url}") from last_exc


# ── 并发下载 ──────────────────────────────────────────────────────────────────

def download_results_concurrent(
    tasks: list[tuple],   # [(zip_url, file_node, output_stem), ...]
    keep_zip: bool,
    keep_json: bool,
    proxies: dict | None = None,
    on_done: Callable | None = None,
) -> dict:
    """
    并发下载并解压多个文件的解析结果。

    tasks: [(zip_url, FileNode, output_stem), ...]
    on_done(file_node, md_path_or_None, error_msg_or_None) 回调用于进度更新。
    返回: {FileNode: md_path | None}
    """
    results: dict = {}

    def _process(zip_url: str, fn, stem: str):
        try:
            md_path = extract_result(
                zip_url, fn.path, keep_zip, keep_json, stem, proxies
            )
            return fn, md_path, None
        except Exception as exc:
            logger.error("解压失败 %s: %s", fn.path.name, exc)
            return fn, None, str(exc)

    with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as pool:
        futures = {
            pool.submit(_process, url, fn, stem): fn
            for url, fn, stem in tasks
        }
        for future in as_completed(futures):
            fn, md_path, err = future.result()
            results[fn] = md_path
            if on_done:
                on_done(fn, md_path, err)

    return results


def download_and_extract_zip(zip_url: str, proxies: dict | None = None) -> str | None:
    """
    下载解析结果 ZIP，提取 full.md 文本内容并返回字符串。
    不写入任何文件。失败返回 None。
    用于 PDF 拆分重提后合并 markdown。
    """
    try:
        zip_bytes = _download_with_retry(zip_url, proxies)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                if Path(name).name == "full.md":
                    return zf.read(name).decode("utf-8", errors="replace")
        logger.warning("download_and_extract_zip: ZIP 中未找到 full.md: %s", zip_url[:80])
        return None
    except Exception as exc:
        logger.error("download_and_extract_zip 失败: %s", exc)
        return None


def download_chunk_zip(
    zip_url: str,
    out_dir: Path,
    chunk_stem: str,
    keep_zip: bool,
    keep_json: bool,
    proxies: dict | None = None,
) -> str | None:
    """
    下载单个分片的解析结果 ZIP，完整处理所有内容：
    - full.md 文本以字符串返回（由调用方合并后写入）
    - images/* 写入 out_dir/images/（与正常文件共享同一 images 目录）
    - keep_zip=True 时将 ZIP 保存为 out_dir/{chunk_stem}.zip
    - keep_json=True 时保存 JSON 文件到 out_dir/
    失败返回 None。
    """
    try:
        zip_bytes = _download_with_retry(zip_url, proxies)
    except Exception as exc:
        logger.error("download_chunk_zip 下载失败: %s", exc)
        return None

    if keep_zip:
        try:
            (out_dir / f"{chunk_stem}.zip").write_bytes(zip_bytes)
            logger.debug("已保存分片 ZIP: %s", chunk_stem)
        except OSError as exc:
            logger.warning("保存分片 ZIP 失败: %s", exc)

    images_dir = out_dir / "images"
    md_text: str | None = None

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                p = Path(name)
                fname = p.name
                parts = p.parts

                if fname == "full.md":
                    md_text = zf.read(name).decode("utf-8", errors="replace")
                    continue

                if len(parts) >= 2 and parts[0] == "images":
                    try:
                        images_dir.mkdir(parents=True, exist_ok=True)
                        dest = images_dir / fname
                        dest.write_bytes(zf.read(name))
                    except OSError as exc:
                        logger.warning("写入分片图片失败 %s: %s", fname, exc)
                    continue

                if fname.endswith("_origin.pdf"):
                    continue

                if fname.endswith(".json") and keep_json:
                    try:
                        out_dir.mkdir(parents=True, exist_ok=True)
                        (out_dir / fname).write_bytes(zf.read(name))
                    except OSError as exc:
                        logger.warning("写入分片 JSON 失败 %s: %s", fname, exc)
                    continue
    except Exception as exc:
        logger.error("download_chunk_zip 解压失败: %s", exc)
        return None

    if md_text is None:
        logger.warning("download_chunk_zip: ZIP 中未找到 full.md: %s", zip_url[:80])
    return md_text
