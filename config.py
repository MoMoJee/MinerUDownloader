"""
config.py — YAML 配置加载 / 保存，以及全局常量。
配置文件 mineru_config.yaml 始终写在脚本所在目录。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# ── 路径 ────────────────────────────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = _SCRIPT_DIR / "mineru_config.yaml"

# ── API 常量 ─────────────────────────────────────────────────────────────────
API_BASE = "https://mineru.net"
BATCH_URL = f"{API_BASE}/api/v4/file-urls/batch"
RESULT_URL = f"{API_BASE}/api/v4/extract-results/batch"

# ── 支持的文件类型 ────────────────────────────────────────────────────────────
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf",
    ".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp",
    ".doc", ".docx",
    ".ppt", ".pptx",
})

# ── 并发 / 重试常量 ───────────────────────────────────────────────────────────
MAX_UPLOAD_WORKERS = 4
MAX_DOWNLOAD_WORKERS = 4
DOWNLOAD_RETRY = 3
INTER_BATCH_DELAY = 2       # 批次间延迟（秒）
HTTP_TIMEOUT = 30           # requests 超时（秒）

# ── 默认配置值 ────────────────────────────────────────────────────────────────
_DEFAULTS: dict[str, Any] = {
    "token": "",
    "language": "ch",
    "proxy_mode": "system",   # system / custom / none
    "proxy_url": "",
    "batch_size": 20,
    "poll_interval": 8,
    "timeout": 1800,
    "keep_zip": False,
    "keep_json": False,
    "duplicate_default": None,  # overwrite / skip / rename / null
}

_YAML_HEADER = """\
# MinerU Downloader 持久化配置
# 由程序自动生成/更新，也可手动编辑

"""


def load_config() -> dict[str, Any]:
    """
    读取 YAML 配置。若文件不存在，则写入默认值后返回。
    缺失的键会用默认值补全（向前兼容）。
    """
    if not CONFIG_FILE.exists():
        cfg = dict(_DEFAULTS)
        save_config(cfg)
        return cfg

    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        loaded: dict = yaml.safe_load(f) or {}

    # 用默认值补全缺失键
    cfg = dict(_DEFAULTS)
    cfg.update({k: v for k, v in loaded.items() if k in _DEFAULTS})
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    """将配置字典写入 YAML 文件。"""
    # 只保存已知键，避免写入垃圾数据
    filtered = {k: cfg.get(k, _DEFAULTS[k]) for k in _DEFAULTS}
    content = _YAML_HEADER + yaml.dump(
        filtered,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    CONFIG_FILE.write_text(content, encoding="utf-8")


def merge_cli_args(cfg: dict[str, Any], args: Any) -> dict[str, Any]:
    """
    用 argparse Namespace 中明确设置的参数覆盖 YAML 配置。
    返回最终生效配置字典（不修改原 cfg）。
    """
    result = dict(cfg)

    # token
    if getattr(args, "token", None):
        result["token"] = args.token

    # proxy
    if getattr(args, "no_proxy", False):
        result["proxy_mode"] = "none"
    elif getattr(args, "proxy", None):
        result["proxy_mode"] = "custom"
        result["proxy_url"] = args.proxy

    # 数值型选项
    for attr, key in [
        ("language", "language"),
        ("batch_size", "batch_size"),
        ("poll_interval", "poll_interval"),
        ("timeout", "timeout"),
    ]:
        val = getattr(args, attr, None)
        if val is not None:
            result[key] = val

    return result


def build_proxies(mode: str, custom_url: str = "") -> dict[str, str] | None:
    """
    根据代理模式构建 requests proxies 字典。

    mode:
      "system" → None（让 requests 读取系统环境变量 HTTP_PROXY / HTTPS_PROXY）
      "custom" → {"http": custom_url, "https": custom_url}
      "none"   → {"http": "", "https": ""}（强制绕过代理）
    """
    if mode == "system":
        return None
    if mode == "custom" and custom_url:
        return {"http": custom_url, "https": custom_url}
    # "none" 或 custom 但 url 为空
    return {"http": "", "https": ""}


def build_curl_proxy_args(mode: str, custom_url: str = "") -> list[str]:
    """
    为 curl.exe 生成代理参数列表。
    用于上传文件时传给 subprocess。
    """
    if mode == "custom" and custom_url:
        return ["-x", custom_url]
    if mode == "none":
        return ["--noproxy", "*"]
    # "system"：curl 默认读取 HTTPS_PROXY 环境变量，无需额外参数
    return []
