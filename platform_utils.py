"""
platform_utils.py — 跨平台兼容辅助函数。

集中处理 Windows / Linux / macOS 的差异（目前主要是 curl 可执行文件查找）。
"""
from __future__ import annotations

import os
import shutil
import sys

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def find_curl() -> str | None:
    """
    返回可用的 curl 可执行文件路径；找不到时返回 None。

    Windows 10/11 自带 curl.exe；Linux / macOS 一般只提供 curl。
    优先使用与当前平台匹配的名称，避免在 Windows 上误选其他同名程序。
    """
    names = ("curl.exe", "curl") if IS_WINDOWS else ("curl",)
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def curl_install_hint() -> str:
    """返回当前平台安装 curl 的提示文本。"""
    if IS_WINDOWS:
        return (
            "Windows 10 / 11 已内置 curl.exe，请确认其在 PATH 中；\n"
            "或从 https://curl.se/download.html 下载。"
        )
    if IS_MACOS:
        return "macOS 通常已内置 curl；如缺失可运行：brew install curl"
    return (
        "请安装 curl，例如：\n"
        "  Debian / Ubuntu : sudo apt install curl\n"
        "  Fedora          : sudo dnf install curl\n"
        "  Arch Linux      : sudo pacman -S curl"
    )
