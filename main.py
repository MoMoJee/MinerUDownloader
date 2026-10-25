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
from typing import Any


def _force_utf8_stdio() -> None:
    """让管道 / 重定向场景默认使用 UTF-8，避免 agent 或脚本读到乱码。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if encoding in ("utf8", "utf8mb4"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdio()

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
        logging.FileHandler(_LOG_FILE, encoding="utf-8", mode="a", delay=True),
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
  python main.py D:\\docs                    # TUI 模式
  python main.py D:\\docs --cli              # 一次性非交互执行
  python main.py D:\\docs --cli --dry-run --json
                                             # 只扫描，输出 JSON（不调用 API）
  python main.py D:\\docs --cli --file-list files.txt
                                             # 只处理清单中的文件
  python main.py D:\\docs --cli --background  # 后台执行，返回 job_id
  python main.py --job-wait <job_id>          # 等待后台任务并输出结果
  python main.py D:\\docs --interactive      # 旧版交互式 CLI 向导
  python main.py . --no-proxy --save-config  # 关闭代理并保存配置
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
        help="一次性非交互模式（agent / 脚本；所有参数需一次给全）",
    )
    mode_group.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="旧版交互式 CLI 向导（仅人类使用）",
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

    # 非交互 / 自动化
    parser.add_argument(
        "-y", "--yes",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,  # 已废弃：--cli 现在默认就是非交互
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        dest="dry_run",
        help="只扫描并输出将要处理的文件，不提交 API、不需要 Token",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="以 JSON 输出扫描/处理结果",
    )
    parser.add_argument(
        "--duplicate",
        choices=["overwrite", "skip", "rename"],
        default=None,
        help="已有同名 .md 的处理策略（默认读取配置，否则 skip）",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="只处理匹配的文件（相对路径或文件名，可重复）",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="排除匹配的文件（相对路径或文件名，可重复）",
    )
    parser.add_argument(
        "--file-list",
        default=None,
        metavar="PATH",
        dest="file_list",
        help="只处理清单中的文件（一行一个，相对根目录或绝对路径；# 开头为注释）",
    )

    # 后台 / 作业控制
    parser.add_argument(
        "--background",
        action="store_true",
        default=False,
        help="后台执行一次性任务，立即返回 job_id",
    )
    parser.add_argument(
        "--job-status",
        default=None,
        metavar="ID",
        dest="job_status",
        help="查看后台任务状态（输出 JSON）",
    )
    parser.add_argument(
        "--job-wait",
        default=None,
        metavar="ID",
        dest="job_wait",
        help="等待后台任务结束，然后输出结果（输出 JSON）",
    )
    parser.add_argument(
        "--job-result",
        default=None,
        metavar="ID",
        dest="job_result",
        help="读取已完成后台任务的结果（输出 JSON）",
    )
    parser.add_argument(
        "--job-timeout",
        type=int,
        default=None,
        metavar="SEC",
        dest="job_timeout",
        help="--job-wait 的超时秒数（默认 3600，0 表示一直等待）",
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


# ── 后台作业 ──────────────────────────────────────────────────────────────────

def _job_error(message: str, job_id: str | None = None) -> int:
    from jobs import emit

    payload = {"success": False, "error": message}
    if job_id:
        payload["job_id"] = job_id
    emit(payload)
    return 2


def _handle_job_control(args: Any) -> int | None:
    job_flags = (args.job_status, args.job_wait, args.job_result)
    job_id = args.job_status
    if job_id is None:
        job_id = args.job_wait
    if job_id is None:
        job_id = args.job_result
    count = sum(1 for value in job_flags if value is not None)

    if args.job_timeout is not None and args.job_wait is None:
        return _job_error("--job-timeout 只能与 --job-wait 一起使用")
    if count > 1:
        return _job_error("--job-status / --job-wait / --job-result 只能使用一个")
    if job_id is None:
        return None
    if args.background:
        return _job_error("--background 不能与 job 控制参数同时使用", job_id)
    if args.job_timeout is not None and args.job_timeout < 0:
        return _job_error("--job-timeout 不能为负数", job_id)

    from jobs import emit, get_result, get_status, wait_job

    try:
        if args.job_status is not None:
            meta = get_status(job_id)
            emit(meta)
            return 0 if meta.get("status") in ("running", "done") else 1

        if args.job_wait is not None:
            timeout = 3600 if args.job_timeout is None else args.job_timeout
            meta = wait_job(job_id, timeout)
            if meta.get("status") not in ("done", "failed"):
                emit(meta)
                return 2
        result, meta = get_result(job_id)
        emit(result)
        if meta.get("exit_code") is not None:
            return int(meta["exit_code"])
        return 0 if meta.get("status") == "done" else 1
    except (FileNotFoundError, ValueError, RuntimeError, OSError) as exc:
        emit({"success": False, "job_id": job_id, "error": str(exc)})
        return 2


def _handle_background(args: Any) -> int | None:
    if not args.background:
        return None

    from jobs import emit, public_status, start_background

    if args.tui or args.interactive:
        emit({"success": False, "error": "--background 不能与 --tui / --interactive 同时使用"})
        return 2
    if args.dry_run:
        emit({"success": False, "error": "--background 不能与 --dry-run 同时使用"})
        return 2
    if (
        args.job_status is not None
        or args.job_wait is not None
        or args.job_result is not None
        or args.job_timeout is not None
    ):
        emit({"success": False, "error": "--background 不能与 job 控制参数同时使用"})
        return 2

    root_dir = Path(args.root_dir).resolve()
    if not root_dir.is_dir():
        emit({"success": False, "error": f"目录不存在或不是目录: {root_dir}"})
        return 2

    child_args = [arg for arg in sys.argv[1:] if arg != "--background"]
    try:
        meta = start_background(child_args, root_dir)
    except Exception as exc:
        emit({"success": False, "error": f"启动后台任务失败: {exc}"})
        return 2

    emit({"success": True, **public_status(meta)})
    return 0


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    for handler in (_handle_job_control, _handle_background):
        code = handler(args)
        if code is not None:
            sys.exit(code)

    _logger = logging.getLogger("main")
    _logger.debug("=== 启动 MinerU Downloader ===")
    _logger.debug("sys.frozen=%s  sys.executable=%s", getattr(__import__('sys'), 'frozen', False), sys.executable)
    _logger.debug("__file__=%s", __file__)
    _logger.debug(
        "args=%s",
        {k: ("***" if k == "token" and v else v) for k, v in vars(args).items()},
    )

    # 1. 检查前置依赖（dry-run 不提交 API，无需 curl）
    if not args.dry_run:
        _check_prerequisites()

    # 2. 加载 YAML 配置，用命令行参数覆盖
    cfg = load_config(write_if_missing=not args.dry_run)
    _logger.debug("CONFIG_FILE=%s", __import__('config').CONFIG_FILE)
    _logger.debug("cfg (after load_config)= token_count=%s lb=%s language=%s proxy=%s",
                  len(__import__('token_manager').TokenManager.parse_tokens(cfg.get('token', ''))),
                  cfg.get('lb_enabled'), cfg.get('language'), cfg.get('proxy_mode'))

    # Token 优先级：mineru_config.yaml < 环境变量 MINERU_TOKEN < --token 参数
    # （交互向导内部输入优先级最高，在 cli.py 中处理）
    env_token = os.environ.get("MINERU_TOKEN")
    if env_token:
        cfg["token"] = env_token
        _logger.debug("cfg: token 来自环境变量 MINERU_TOKEN（已脱敏：%s...）",
                      env_token[:8])

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
        print("配置已保存到 mineru_config.yaml",
              file=sys.stderr if args.json else sys.stdout)

    # 4. 确定根目录
    raw_root = args.root_dir
    if args.yes:
        print(
            "[提示] --yes 已废弃：--cli 现在默认就是一次性非交互模式。",
            file=sys.stderr,
        )
    if args.interactive and (
        args.dry_run or args.json or args.file_list is not None or args.yes
    ):
        print(
            "[错误] --interactive 不能与 --dry-run / --json / --file-list / --yes 同时使用",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.tui and (
        args.dry_run or args.json or args.yes
        or args.duplicate or args.include or args.exclude
        or args.file_list is not None
    ):
        print(
            "[错误] --dry-run / --json / --yes / --duplicate / --include / --exclude / --file-list "
            "仅适用于 --cli 或 --interactive 模式",
            file=sys.stderr,
        )
        sys.exit(2)
    use_cli = (
        args.cli or args.interactive or args.dry_run or args.json or args.yes
        or args.file_list is not None or args.duplicate is not None
        or bool(args.include) or bool(args.exclude)
    )

    if use_cli:
        # CLI 模式：必须有明确目录（或默认当前目录），同步扫描
        root_dir = Path(raw_root).resolve()
        if not root_dir.exists() or not root_dir.is_dir():
            print(f"[错误] 目录不存在或不是目录: {root_dir}", file=sys.stderr)
            sys.exit(2)
        root_node = scan(root_dir)
        if root_node.file_count == 0:
            print("未在指定目录下找到任何支持的文件。", file=sys.stderr)
            sys.exit(2)
        from cli import run_cli
        exit_code = run_cli(
            root_node,
            cfg,
            interactive=args.interactive,
            dry_run=args.dry_run,
            json_output=args.json,
            duplicate=args.duplicate,
            include=args.include,
            exclude=args.exclude,
            file_list=args.file_list,
        )
        sys.exit(exit_code)
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
                "或使用 --cli 参数以一次性非交互模式运行。",
                file=sys.stderr,
            )
            sys.exit(1)
        run_tui(initial_dir, cfg, show_picker=show_picker)


if __name__ == "__main__":
    main()
