"""
cli.py — 纯命令行交互模式（rich 格式化输出 + input() 向导）。
提供与 TUI 完全等价的功能，适合 SSH / 脚本调用场景。
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.tree import Tree as RichTree

from scanner import (
    DirNode,
    DuplicateAction,
    FileNode,
    flatten_files,
    resolve_output_stem,
)
from config import build_proxies, save_config
from token_manager import TokenManager

console = Console()
logger = logging.getLogger(__name__)


# ── 工具 ──────────────────────────────────────────────────────────────────────

def _fmt_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}" if unit != "B" else f"{b} B"
        b /= 1024
    return f"{b:.1f} TB"


def _ask(prompt: str, default: str = "") -> str:
    """带默认值的 input()，Ctrl+C 优雅退出。"""
    try:
        ans = input(prompt)
        return ans.strip() if ans.strip() else default
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]已取消，退出程序。[/yellow]")
        sys.exit(0)


def _ask_choice(prompt: str, valid: set[str], default: str = "") -> str:
    """循环提示直到输入合法选项（不区分大小写）。"""
    while True:
        ans = _ask(prompt, default).upper()
        if ans in valid:
            return ans
        console.print(f"[red]无效输入，请输入 {'/'.join(sorted(valid))}[/red]")


def _parse_exclude_input(text: str, max_idx: int) -> set[int]:
    """
    解析排除序号字符串，返回要排除的 1-based 序号集合。
    支持：逗号分隔、范围（如 3-5）、"all"
    """
    if text.lower() == "all":
        return set(range(1, max_idx + 1))
    excluded: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                lo, hi = part.split("-", 1)
                lo, hi = int(lo.strip()), int(hi.strip())
                excluded.update(range(lo, hi + 1))
            except ValueError:
                console.print(f"[yellow]忽略无效范围: {part}[/yellow]")
        else:
            try:
                excluded.add(int(part))
            except ValueError:
                console.print(f"[yellow]忽略无效序号: {part}[/yellow]")
    return {i for i in excluded if 1 <= i <= max_idx}


# ── 阶段一：扫描展示 ──────────────────────────────────────────────────────────

def _print_file_tree(all_files: list[FileNode], root: DirNode) -> None:
    """用 rich.Tree 打印目录结构，附带序号。"""
    idx_map: dict[int, FileNode] = {}  # 1-based序号 → FileNode
    counter = [0]

    def _build_rich_tree(node: DirNode, rtree: RichTree) -> None:
        for child in node.children:
            if isinstance(child, DirNode):
                sub = rtree.add(f"[bold blue]📁 {child.path.name}/[/bold blue]")
                _build_rich_tree(child, sub)
            else:
                counter[0] += 1
                idx = counter[0]
                idx_map[idx] = child
                dup_tag = ""
                if child.has_duplicate:
                    dup_tag = f" [yellow]⚠ [已有 {child.existing_md.name}][/yellow]"
                size_str = _fmt_size(child.size)
                rtree.add(
                    f"[dim]{idx:>3}[/dim]  {child.path.name}{dup_tag}  [green]{size_str}[/green]"
                )

    root_tree = RichTree(f"[bold]📁 {root.path.name or root.path}[/bold]")
    _build_rich_tree(root, root_tree)
    console.print(root_tree)
    return idx_map


# ── 阶段二：文件选择 ──────────────────────────────────────────────────────────

def _phase_select(root: DirNode) -> list[FileNode]:
    """展示文件树，收集排除序号，返回已选文件列表。"""
    all_files = flatten_files(root)
    if not all_files:
        console.print("[red]未找到任何支持的文件。[/red]")
        sys.exit(0)

    total_size = sum(f.size for f in all_files)
    dup_count = sum(1 for f in all_files if f.has_duplicate)

    console.rule("[bold cyan]MinerU 批量解析下载工具[/bold cyan]")
    console.print(
        f"[bold]扫描目录:[/bold] {root.path}\n"
        f"找到 [green]{len(all_files)}[/green] 个可解析文件"
        f"（共 [cyan]{_fmt_size(total_size)}[/cyan]）"
        + (f"  [yellow]⚠ {dup_count} 个已有 .md 输出[/yellow]" if dup_count else "")
    )
    console.print()

    idx_map = _print_file_tree(all_files, root)

    console.print()
    console.print("[bold][选择文件][/bold]")
    console.print("输入要 [red]【排除】[/red] 的序号（支持逗号和范围，如 '3,6-7'），直接回车保持全选，输入 'all' 全不选：")

    while True:
        raw = _ask("> ")
        if not raw:
            selected = list(all_files)
            break
        excluded = _parse_exclude_input(raw, len(all_files))
        selected = [f for i, f in enumerate(all_files, 1) if i not in excluded]
        if not selected:
            console.print("[yellow]没有选中任何文件，请重新输入。[/yellow]")
            continue
        break

    # 标记
    sel_set = set(id(f) for f in selected)
    for f in all_files:
        f.selected = id(f) in sel_set

    sel_size = sum(f.size for f in selected)
    dup_sel = sum(1 for f in selected if f.has_duplicate)
    console.print(
        f"\n[bold green]已选中 {len(selected)} 个文件"
        f"（{_fmt_size(sel_size)}）[/bold green]"
        + (f"  [yellow]⚠ {dup_sel} 个有重复 .md[/yellow]" if dup_sel else "")
    )
    return selected


# ── 阶段三：重复文件处理 ──────────────────────────────────────────────────────

def _phase_duplicate(selected: list[FileNode]) -> list[FileNode]:
    """处理重复文件的决策，返回更新了 duplicate_action 的列表。"""
    dup_files = [f for f in selected if f.has_duplicate]
    if not dup_files:
        return selected

    console.print()
    console.rule("[bold yellow]重复文件处理[/bold yellow]")

    if len(dup_files) > 5:
        console.print(f"有 [yellow]{len(dup_files)}[/yellow] 个文件存在重复 .md，请选择统一处理方式：")
        console.print("  (A) 全部覆盖 — 删除旧 .md，重新解析写入")
        console.print("  (K) 全部跳过 — 保留旧 .md，不提交解析")
        console.print("  (N) 全部改名 — 输出为 {stem}_1.md")
        console.print("  (M) 逐一决定")
        choice = _ask_choice("选择 [A/K/N/M]: ", {"A", "K", "N", "M"})
        if choice != "M":
            action_map = {
                "A": DuplicateAction.OVERWRITE,
                "K": DuplicateAction.SKIP,
                "N": DuplicateAction.RENAME,
            }
            for f in dup_files:
                f.duplicate_action = action_map[choice]
            return selected

    # 逐一询问
    console.print("以下文件已有对应的 .md 输出，请逐一选择处理方式：\n")
    for i, fn in enumerate(dup_files, 1):
        console.print(
            f"  [{i}/{len(dup_files)}] [cyan]{fn.rel_path}[/cyan]"
            f" → 已有 [dim]{fn.existing_md}[/dim]"
        )
        console.print("        (O) 覆盖  (S) 跳过  (R) 改名")
        c = _ask_choice("  选择 [O/S/R]: ", {"O", "S", "R"})
        fn.duplicate_action = {
            "O": DuplicateAction.OVERWRITE,
            "S": DuplicateAction.SKIP,
            "R": DuplicateAction.RENAME,
        }[c]
        console.print()

    return selected


# ── 阶段四：选项确认 ──────────────────────────────────────────────────────────

def _phase_options(cfg: dict) -> dict:
    """展示当前配置，询问是否修改，返回最终生效配置。"""
    console.print()
    console.rule("[bold cyan]解析选项[/bold cyan]")

    token_raw = cfg.get("token", "")
    if isinstance(token_raw, list):
        token_raw = ",".join(token_raw)
    tokens = TokenManager.parse_tokens(token_raw)
    if len(tokens) > 1:
        token_display = f"{len(tokens)} 个 Token"
    elif tokens:
        t0 = tokens[0]
        token_display = f"{t0[:6]}...{t0[-4:]}" if len(t0) > 10 else "(已配置)"
    else:
        token_display = "[red](未配置)[/red]"
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold", width=14)
    table.add_column()
    table.add_row("语言", cfg.get("language", "ch"))
    table.add_row("保留 ZIP", "是" if cfg.get("keep_zip") else "否")
    table.add_row("保留 JSON", "是" if cfg.get("keep_json") else "否")
    table.add_row("代理模式", cfg.get("proxy_mode", "system"))
    table.add_row("代理地址", cfg.get("proxy_url", "") or "(无)")
    table.add_row("每批文件数", str(cfg.get("batch_size", 20)))
    table.add_row("Token", token_display)
    table.add_row("负载均衡", "开" if cfg.get("lb_enabled") else "关（自动）")
    console.print(table)

    ans = _ask("\n是否修改任意选项？(y/N): ", "N").upper()
    if ans != "Y":
        # 若 Token 未配置则强制输入
        if not TokenManager.parse_tokens(cfg.get("token", "")):
            cfg["token"] = _ask("请输入 MinerU Token（多个用逗号分隔）: ")
        return cfg

    cfg = dict(cfg)
    console.print()

    # Token
    t = _ask(f"  Token（逗号分隔多个，回车保持当前）: ")
    if t:
        cfg["token"] = t
    # 负载均衡
    current_tokens = TokenManager.parse_tokens(cfg.get("token", ""))
    if len(current_tokens) > 1:
        lb_default = "Y" if cfg.get("lb_enabled", True) else "N"
        lb = _ask(f"  启用负载均衡（当前: {'Y' if cfg.get('lb_enabled', True) else 'N'}）(Y/n): ", lb_default).upper()
        cfg["lb_enabled"] = lb != "N"

    # 语言
    lang = _ask(f"  语言（当前: {cfg.get('language', 'ch')}，回车保持）: ")
    if lang:
        cfg["language"] = lang

    # keep_zip
    kz = _ask(f"  保留 ZIP（当前: {'是' if cfg.get('keep_zip') else '否'}）(y/N): ", "N").upper()
    cfg["keep_zip"] = kz == "Y"

    # keep_json
    kj = _ask(f"  保留 JSON（当前: {'是' if cfg.get('keep_json') else '否'}）(y/N): ", "N").upper()
    cfg["keep_json"] = kj == "Y"

    # 代理
    proxy_mode = _ask(
        f"  代理模式（当前: {cfg.get('proxy_mode', 'system')}）[system/custom/none，回车保持]: "
    ) or cfg.get("proxy_mode", "system")
    if proxy_mode in ("system", "custom", "none"):
        cfg["proxy_mode"] = proxy_mode
    if proxy_mode == "custom":
        pu = _ask(f"  代理地址（当前: {cfg.get('proxy_url', '')}，回车保持）: ")
        if pu:
            cfg["proxy_url"] = pu

    save_ans = _ask("\n  是否将以上修改保存到 mineru_config.yaml？(y/N): ", "N").upper()
    if save_ans == "Y":
        save_config(cfg)
        console.print("[green]配置已保存。[/green]")

    if not TokenManager.parse_tokens(cfg.get("token", "")):
        cfg["token"] = _ask("请输入 MinerU Token（必填，多个用逗号分隔）: ")

    return cfg


# ── 阶段五：确认 ──────────────────────────────────────────────────────────────

def _phase_confirm(selected: list[FileNode], cfg: dict) -> bool:
    """展示处理摘要，询问用户确认。返回 True 表示确认开始。"""
    to_process = [f for f in selected if f.duplicate_action != DuplicateAction.SKIP]
    skipped = [f for f in selected if f.duplicate_action == DuplicateAction.SKIP]
    batch_size = int(cfg.get("batch_size", 20))
    import math
    n_batches = math.ceil(len(to_process) / batch_size) if to_process else 0

    console.print()
    console.rule("[bold green]确认开始[/bold green]")
    console.print(
        f"  将提交 [green]{len(to_process)}[/green] 个文件进行解析"
        f"（分 [cyan]{n_batches}[/cyan] 批，每批最多 {batch_size} 个）"
    )
    if skipped:
        console.print(f"  跳过 [yellow]{len(skipped)}[/yellow] 个文件（保留旧 .md）")

    ans = _ask("\n确认开始解析？(Y/n): ", "Y").upper()
    return ans != "N"


# ── 阶段六：处理进度 ──────────────────────────────────────────────────────────

class _CliProgressState:
    def __init__(self):
        self.lock = threading.Lock()
        self.file_status: dict[FileNode, str] = {}
        self.file_msg: dict[FileNode, str] = {}
        self.log: list[str] = []
        self.done = 0
        self.failed = 0
        self.finished = False

    def set(self, fn: FileNode, status: str, msg: str = "") -> None:
        with self.lock:
            self.file_status[fn] = status
            self.file_msg[fn] = msg

    def log_line(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        with self.lock:
            self.log.append(f"[{ts}] {msg}")
        logger.info(msg)


def _phase_process(selected: list[FileNode], cfg: dict) -> None:
    """提交、轮询、下载，rich Progress 展示进度，最终打印汇总。"""
    from api import build_batches, submit_and_upload_batch, poll_batches_concurrent
    from processor import download_results_concurrent

    state = _CliProgressState()
    to_process = [f for f in selected if f.duplicate_action != DuplicateAction.SKIP]
    skipped = [f for f in selected if f.duplicate_action == DuplicateAction.SKIP]

    for fn in to_process:
        state.set(fn, "waiting", "等待中")

    # 构建 TokenManager
    tokens = TokenManager.parse_tokens(cfg.get("token", ""))
    lb = cfg.get("lb_enabled")
    if lb is None:
        lb = len(tokens) > 1
    token_manager = TokenManager(tokens, lb_enabled=bool(lb))
    console.print(f"[dim]Token: {' | '.join(tm.display for tm in token_manager.entries)}[/dim]")

    proxies = build_proxies(cfg.get("proxy_mode", "system"), cfg.get("proxy_url", ""))
    proxy_mode = cfg.get("proxy_mode", "system")
    proxy_url = cfg.get("proxy_url", "")
    batch_size = int(cfg.get("batch_size", 20))
    poll_interval = int(cfg.get("poll_interval", 8))
    timeout = int(cfg.get("timeout", 1800))
    keep_zip = cfg.get("keep_zip", False)
    keep_json = cfg.get("keep_json", False)
    language = cfg.get("language", "ch")

    # 预处理：OVERWRITE 删除旧 .md
    for fn in to_process:
        if fn.duplicate_action == DuplicateAction.OVERWRITE and fn.existing_md:
            try:
                fn.existing_md.unlink(missing_ok=True)
            except OSError as e:
                state.log_line(f"[警告] 无法删除 {fn.existing_md}: {e}")

    batches = build_batches(to_process, batch_size)
    state.log_line(f"共 {len(to_process)} 个文件，分 {len(batches)} 批")

    batch_node_map: dict[str, list[FileNode]] = {}
    batch_token_idx: dict[str, int] = {}
    stem_map: dict[FileNode, str] = {}

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    )

    with progress:
        upload_task = progress.add_task("上传文件", total=len(to_process))
        parse_task = progress.add_task("等待解析", total=len(to_process))
        dl_task = progress.add_task("下载结果", total=len(to_process))

        # ── 上传 ──
        uploaded_total = 0
        for i, batch in enumerate(batches):
            state.log_line(f"第 {i+1}/{len(batches)} 批：上传 {len(batch)} 个文件")
            for fn in batch:
                state.set(fn, "uploading", "上传中...")
                stem_map[fn] = resolve_output_stem(fn)

            def _on_up(path: Path, ok: bool, _batch=batch):
                fn_match = next((f for f in _batch if f.path == path), None)
                if not fn_match:
                    return
                if ok:
                    state.set(fn_match, "uploaded", "上传完成，等待解析")
                else:
                    state.set(fn_match, "failed", "上传失败")
                    with state.lock:
                        state.failed += 1
                    state.log_line(f"[失败] 上传 {path.name}")
                progress.advance(upload_task)

            try:
                bid, upload_results, token_idx = submit_and_upload_batch(
                    batch, token_manager, "vlm", language, proxies,
                    proxy_mode, proxy_url, on_uploaded=_on_up
                )
                success_nodes = [fn for fn in batch if upload_results.get(fn.path, False)]
                batch_node_map[bid] = success_nodes
                batch_token_idx[bid] = token_idx
                for fn in success_nodes:
                    state.set(fn, "parsing", "排队中")
            except Exception as exc:
                state.log_line(f"[错误] 批次 {i+1} 失败: {exc}")
                for fn in batch:
                    if state.file_status.get(fn) == "uploading":
                        state.set(fn, "failed", f"失败: {exc}")
                        with state.lock:
                            state.failed += 1
                        progress.advance(upload_task)

        # ── 轮询 ──
        progress.update(parse_task, description="解析中")
        state.log_line("轮询解析结果中...")

        def on_bp(bid: str, results: list[dict]) -> None:
            nodes = batch_node_map.get(bid, [])
            name_map = {fn.path.name: fn for fn in nodes}
            for r in results:
                fn = name_map.get(r.get("file_name", ""))
                if fn is None:
                    continue
                s = r.get("state", "")
                if s == "running":
                    prog = r.get("extract_progress", {})
                    state.set(fn, "parsing",
                               f"解析中 ({prog.get('extracted_pages','?')}/{prog.get('total_pages','?')} 页)")
                elif s == "done":
                    state.set(fn, "dl-ready", "准备下载")
                    progress.advance(parse_task)
                elif s == "failed":
                    state.set(fn, "failed", f"解析失败: {r.get('err_msg','')}")
                    with state.lock:
                        state.failed += 1
                    state.log_line(f"[失败] {fn.path.name}: {r.get('err_msg','')}")
                    progress.advance(parse_task)

        batch_token_pairs = [(bid, batch_token_idx.get(bid, 0)) for bid in batch_node_map]
        poll_results = poll_batches_concurrent(
            batch_token_pairs, token_manager,
            poll_interval, timeout, proxies, on_bp
        )

        # ── 下载 ──
        progress.update(dl_task, description="下载结果")
        download_tasks = []
        for bid, extract_results in poll_results.items():
            nodes = batch_node_map.get(bid, [])
            name_map = {fn.path.name: fn for fn in nodes}
            for r in extract_results:
                fn = name_map.get(r.get("file_name", ""))
                if fn and r.get("state") == "done" and r.get("full_zip_url"):
                    download_tasks.append((r["full_zip_url"], fn, stem_map[fn]))

        if download_tasks:
            state.log_line(f"下载 {len(download_tasks)} 个结果...")

            def on_dl(fn: FileNode, md_path, err: str | None) -> None:
                if md_path:
                    state.set(fn, "done", f"→ {md_path.name}")
                    with state.lock:
                        state.done += 1
                    state.log_line(f"[完成] {fn.path.name} → {md_path.name}")
                else:
                    state.set(fn, "failed", f"下载失败: {err}")
                    with state.lock:
                        state.failed += 1
                    state.log_line(f"[失败] 下载 {fn.path.name}: {err}")
                progress.advance(dl_task)

            download_results_concurrent(
                download_tasks, keep_zip, keep_json, proxies, on_dl
            )

    # ── 汇总 ──
    console.print()
    console.rule("[bold green]完成汇总[/bold green]")

    result_table = Table(show_header=True, header_style="bold")
    result_table.add_column("状态", width=4)
    result_table.add_column("文件", style="cyan")
    result_table.add_column("输出 / 说明")

    for fn in to_process:
        s = state.file_status.get(fn, "?")
        msg = state.file_msg.get(fn, "")
        if s == "done":
            result_table.add_row("[green]✓[/green]", str(fn.rel_path), f"[green]{msg}[/green]")
        elif s == "failed":
            result_table.add_row("[red]✗[/red]", str(fn.rel_path), f"[red]{msg}[/red]")
        else:
            result_table.add_row("[yellow]?[/yellow]", str(fn.rel_path), msg)

    for fn in skipped:
        result_table.add_row("[dim]—[/dim]", str(fn.rel_path), "[dim]已跳过（保留旧 .md）[/dim]")

    console.print(result_table)
    console.print(
        f"\n[bold]成功: [green]{state.done}[/green]  "
        f"失败: [red]{state.failed}[/red]  "
        f"跳过: [yellow]{len(skipped)}[/yellow][/bold]"
    )
    if state.failed:
        console.print("[dim]详细错误已写入 mineru_errors.log[/dim]")

    _ask("\n按回车退出: ")


# ── 主入口 ────────────────────────────────────────────────────────────────────

def run_cli(root_node: DirNode, cfg: dict) -> None:
    """CLI 模式入口，顺序执行各阶段。"""
    selected = _phase_select(root_node)
    selected = _phase_duplicate(selected)
    cfg = _phase_options(cfg)
    if not _phase_confirm(selected, cfg):
        # 退回文件选择
        console.print("[yellow]已取消，重新选择文件…[/yellow]")
        run_cli(root_node, cfg)
        return
    _phase_process(selected, cfg)
