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

from config import load_config, merge_cli_args, save_config
from scanner import scan

# ── 错误日志文件 ──────────────────────────────────────────────────────────────
_LOG_FILE = Path(__file__).parent / "mineru_errors.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_LOG_FILE, encoding="utf-8", mode="a"),
    ],
)
# 控制台只显示 WARNING+
_console_handler = logging.StreamHandler(sys.stderr)
_console_handler.setLevel(logging.WARNING)
logging.getLogger().addHandler(_console_handler)


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

    # 1. 检查前置依赖
    _check_prerequisites()

    # 2. 加载 YAML 配置，用命令行参数覆盖
    cfg = load_config()

    # Token 优先级：yaml < 环境变量 < --token 参数（TUI/CLI 内部输入优先级最高，在各自模块中处理）
    # 若 yaml 没有 token 且环境变量有，则用环境变量
    if not cfg.get("token") and os.environ.get("MINERU_TOKEN"):
        cfg["token"] = os.environ["MINERU_TOKEN"]

    # merge_cli_args 会将 --token 参数（若存在）写入 cfg["token"]，完全替换低优先级来源
    cfg = merge_cli_args(cfg, args)

    # batch_size 合法性检查
    bs = cfg.get("batch_size", 20)
    if not (1 <= int(bs) <= 50):
        print(f"[错误] --batch-size 必须在 1~50 之间，当前值：{bs}", file=sys.stderr)
        sys.exit(1)

    # 3. 可选：回写配置
    if args.save_config:
        save_config(cfg)
        print("配置已保存到 mineru_config.yaml")

    # 4. 扫描目录
    root_dir = Path(args.root_dir).resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        print(f"[错误] 目录不存在或不是目录: {root_dir}", file=sys.stderr)
        sys.exit(1)

    root_node = scan(root_dir)

    if root_node.file_count == 0:
        print("未在指定目录下找到任何支持的文件。", file=sys.stderr)
        sys.exit(0)

    # 5. 分发到 TUI 或 CLI
    use_cli = args.cli  # --tui 是默认，不需要特判

    if use_cli:
        from cli import run_cli
        run_cli(root_node, cfg)
    else:
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
        run_tui(root_node, cfg)


if __name__ == "__main__":
    main()
