"""
main.py — 程序入口。
解析命令行参数，加载配置，扫描目录，分发到 TUI 或 CLI 模式。
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from pathlib import Path

# ── 双击启动检测（Windows） ────────────────────────────────────────────────────
# 打包后双击 exe 时，Windows 会给它创建一个独占的控制台窗口（只有自身进程）。
# 在已有终端中运行时，控制台由多个进程共享（count > 1）。
# 检测到双击后，用 PowerShell 在 exe 父目录的上一级（用户存放文件的目录）打开新终端执行自身。
def _relaunch_if_double_clicked() -> None:
    if not getattr(sys, 'frozen', False):
        return  # 仅打包版处理
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        # GetConsoleProcessList 返回共享当前控制台的进程数量
        # 双击时 == 1（仅自身），从终端运行时 > 1
        buf = (ctypes.c_ulong * 64)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(buf, 64)
        if count != 1:
            return  # 已在终端中，正常运行
    except Exception:
        return  # 获取失败则不干预

    # 双击场景：exe 在 mineru-downloader/ 里，父目录才是用户的工作目录
    exe_path = Path(sys.executable)
    work_dir = exe_path.parent.parent  # mineru-downloader/ 的上一级

    import subprocess
    # 用 PowerShell 在工作目录打开新窗口，执行 exe
    subprocess.Popen(
        [
            'powershell.exe',
            '-NoLogo',
            '-NoExit',
            '-Command', (
                f"Set-Location -LiteralPath '{work_dir}'; "
                f"& '{exe_path}' --folder-picker; "
                "Write-Host ''; "
                "Write-Host ' 程序已结束，按任意键关闭...' -NoNewline; "
                "$null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')"
            ),
        ],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    sys.exit(0)

_relaunch_if_double_clicked()

from config import load_config, merge_cli_args, save_config
from scanner import scan

# ── 错误日志文件 ──────────────────────────────────────────────────────────────
# 打包后 exe 在 _internal/ 中，需用 sys.executable 定位日志到 exe 同级目录
def _log_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent

_LOG_FILE = _log_dir() / "mineru_errors.log"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_LOG_FILE, encoding="utf-8", mode="a"),
    ],
)
# 控制台只显示 WARNING+
_console_handler = logging.StreamHandler(sys.stderr)
_console_handler.setLevel(logging.WARNING)
logging.getLogger().addHandler(_console_handler)
# 抑制过于啰嗦的第三方库 DEBUG 日志
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("charset_normalizer").setLevel(logging.WARNING)


# ── 参数解析 ──────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="MinerU 批量解析下载工具 — 精准解析 API · vlm 模型",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python main.py D:\\docs                    # TUI 模式扫描指定目录
  python main.py D:\\docs --cli              # CLI 模式
  python main.py . --cli --token sk-xxx     # CLI 模式并指定 Token
  python main.py . --no-proxy --save-config # 关闭代理并保存配置
        """,
    )

    # 扫描目录（位置参数，可选）
    parser.add_argument(
        "root_dir",
        nargs="?",
        default=".",
        metavar="ROOT_DIR",
        help="要扫描的根目录（默认：当前目录）",
    )

    # 模式
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--tui",
        action="store_true",
        default=False,
        help="启动 Textual TUI 界面（默认）",
    )
    mode_group.add_argument(
        "--cli",
        action="store_true",
        default=False,
        help="启动纯命令行交互模式（适合 SSH / 脚本）",
    )

    # Token / 代理
    parser.add_argument("--token", metavar="TOKEN", help="MinerU API Token")
    parser.add_argument(
        "--proxy",
        metavar="URL",
        help="HTTP 代理地址，如 http://127.0.0.1:7890",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        default=False,
        dest="no_proxy",
        help="强制不使用任何代理",
    )

    # 解析参数
    parser.add_argument("--language", metavar="LANG", help="文档语言（默认：ch）")
    parser.add_argument(
        "--batch-size",
        type=int,
        metavar="N",
        dest="batch_size",
        help="每批文件数上限（1~50，默认：20）",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        metavar="SEC",
        dest="poll_interval",
        help="轮询间隔秒数（默认：8）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        metavar="SEC",
        help="单批次超时秒数（默认：1800）",
    )

    # 配置
    parser.add_argument(
        "--save-config",
        action="store_true",
        default=False,
        dest="save_config",
        help="将本次命令行参数写回 mineru_config.yaml",
    )
    # 隐藏参数：双击重启时由 _relaunch_if_double_clicked 传入
    parser.add_argument(
        "--folder-picker",
        action="store_true",
        default=False,
        dest="folder_picker",
        help=argparse.SUPPRESS,
    )
    return parser


# ── 启动前检查 ────────────────────────────────────────────────────────────────

def _check_prerequisites() -> None:
    """检查 curl.exe 是否可用（Windows 10+ 内置）。"""
    if shutil.which("curl.exe") is None and shutil.which("curl") is None:
        print(
            "[错误] 未找到 curl / curl.exe。\n"
            "Windows 10 / 11 已内置，请确认其在 PATH 中；\n"
            "或从 https://curl.se/download.html 下载。",
            file=sys.stderr,
        )
        sys.exit(1)


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    _logger = logging.getLogger("main")
    _logger.debug("=== 启动 MinerU Downloader ===")
    _logger.debug("sys.frozen=%s  sys.executable=%s", getattr(__import__('sys'), 'frozen', False), sys.executable)
    _logger.debug("__file__=%s", __file__)
    _logger.debug("args=%s", args)

    # 1. 检查前置依赖
    _check_prerequisites()

    # 2. 加载 YAML 配置，用命令行参数覆盖
    cfg = load_config()
    _logger.debug("CONFIG_FILE=%s", __import__('config').CONFIG_FILE)
    _logger.debug("cfg (after load_config)= token_count=%s lb=%s language=%s proxy=%s",
                  len(__import__('token_manager').TokenManager.parse_tokens(cfg.get('token', ''))),
                  cfg.get('lb_enabled'), cfg.get('language'), cfg.get('proxy_mode'))

    # Token 优先级：yaml < 环境变量 < --token 参数（TUI/CLI 内部输入优先级最高，在各自模块中处理）
    # 若 yaml 没有 token 且环境变量有，则用环境变量
    if not cfg.get("token") and os.environ.get("MINERU_TOKEN"):
        cfg["token"] = os.environ["MINERU_TOKEN"]
        _logger.debug("cfg: token 来自环境变量 MINERU_TOKEN（已脱敏：%s...）",
                      os.environ["MINERU_TOKEN"][:8])

    # merge_cli_args 会将 --token 参数（若存在）写入 cfg["token"]，完全替换低优先级来源
    cfg = merge_cli_args(cfg, args)
    _logger.debug("cfg (after merge_cli_args)= token_count=%s lb=%s",
                  len(__import__('token_manager').TokenManager.parse_tokens(cfg.get('token', ''))),
                  cfg.get('lb_enabled'))

    # batch_size 合法性检查
    bs = cfg.get("batch_size", 20)
    if not (1 <= int(bs) <= 50):
        print(f"[错误] --batch-size 必须在 1~50 之间，当前值：{bs}", file=sys.stderr)
        sys.exit(1)

    # 3. 可选：回写配置
    if args.save_config:
        save_config(cfg)
        print("配置已保存到 mineru_config.yaml")

    # 4. 确定根目录
    raw_root = args.root_dir
    use_cli = args.cli

    if use_cli:
        # CLI 模式：必须有明确目录（或默认当前目录），同步扫描
        root_dir = Path(raw_root).resolve()
        if not root_dir.exists() or not root_dir.is_dir():
            print(f"[错误] 目录不存在或不是目录: {root_dir}", file=sys.stderr)
            sys.exit(1)
        root_node = scan(root_dir)
        if root_node.file_count == 0:
            print("未在指定目录下找到任何支持的文件。", file=sys.stderr)
            sys.exit(0)
        from cli import run_cli
        run_cli(root_node, cfg)
    else:
        # TUI 模式
        # 仅在双击重启（传入 --folder-picker）时显示文件夹选择界面；
        # 在终端直接运行时（即使未指定目录）也直接扫描当前目录。
        show_picker = args.folder_picker
        if show_picker:
            # 初始目录：打包时指向 exe 所在文件夹的父级，开发时指向 cwd
            if getattr(sys, 'frozen', False):
                initial_dir = Path(sys.executable).parent.parent
            else:
                initial_dir = Path.cwd()
        else:
            initial_dir = Path(raw_root).resolve()
            if not initial_dir.exists() or not initial_dir.is_dir():
                print(f"[错误] 目录不存在或不是目录: {initial_dir}", file=sys.stderr)
                sys.exit(1)

        # 5. 启动 TUI
        try:
            from tui import run_tui
        except ImportError as exc:
            print(
                f"[错误] 无法导入 TUI 模块: {exc}\n"
                "请运行 pip install -r requirements.txt 安装依赖，\n"
                "或使用 --cli 参数以命令行模式运行。",
                file=sys.stderr,
            )
            sys.exit(1)
        run_tui(initial_dir, cfg, show_picker=show_picker)


if __name__ == "__main__":
    main()
