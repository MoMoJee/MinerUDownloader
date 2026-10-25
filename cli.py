"""
cli.py — 纯命令行交互模式（rich 格式化输出 + input() 向导）。
提供与 TUI 完全等价的功能，适合 SSH / 脚本调用场景。
"""
from __future__ import annotations

import fnmatch
import io
import json
import logging
import os
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


def _fix_stdio_encoding() -> None:
    """PyInstaller 管道环境下强制 stdout/stderr 为 UTF-8，防止 GBK 编码器拒绝 Unicode 字符。"""
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name)
        if stream.encoding and stream.encoding.lower() in ('gbk', 'cp936', 'cp950'):
            try:
                setattr(sys, name, io.TextIOWrapper(
                    stream.buffer, encoding='utf-8', errors='replace'
                ))
            except Exception:
                pass


_fix_stdio_encoding()

console = Console(legacy_windows=False, force_terminal=sys.stdout.isatty())
logger = logging.getLogger(__name__)

_DUPLICATE_ACTIONS: dict[str, DuplicateAction] = {
    "overwrite": DuplicateAction.OVERWRITE,
    "skip": DuplicateAction.SKIP,
    "rename": DuplicateAction.RENAME,
}


def _print_json(payload: dict) -> None:
    """把机器可读结果写到 stdout，供 agent / 脚本解析。

    使用 ASCII 转义，避免 Windows 控制台 / 管道编码差异导致 JSON 乱码。
    """
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, indent=2) + "\n")
    sys.stdout.flush()


def _matches_pattern(rel_path: Path, pattern: str) -> bool:
    """匹配相对路径或文件名；无斜杠的模式只匹配文件名。"""
    pattern = pattern.replace("\\", "/").strip()
    if not pattern:
        return False
    rel_posix = rel_path.as_posix()
    if fnmatch.fnmatch(rel_posix, pattern):
        return True
    return "/" not in pattern and fnmatch.fnmatch(rel_path.name, pattern)


def _filter_files(
    files: list[FileNode],
    include: list[str] | None,
    exclude: list[str] | None,
) -> list[FileNode]:
    """按 --include / --exclude 的 glob 过滤文件。"""
    result = files
    if include:
        result = [
            f for f in result
            if any(_matches_pattern(f.rel_path, p) for p in include)
        ]
    if exclude:
        result = [
            f for f in result
            if not any(_matches_pattern(f.rel_path, p) for p in exclude)
        ]
    return result


def _duplicate_policy(duplicate: str | None, cfg: dict) -> str:
    """确定非交互模式下的重复 .md 策略：参数 > 配置 > skip。"""
    if duplicate in _DUPLICATE_ACTIONS:
        return duplicate
    configured = cfg.get("duplicate_default")
    if configured in _DUPLICATE_ACTIONS:
        return configured
    return "skip"


def _load_file_list(list_path: Path, files: list[FileNode]) -> list[FileNode]:
    """读取 --file-list，返回扫描结果中匹配的文件（保持扫描顺序）。"""
    try:
        text = list_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise RuntimeError(f"无法读取 --file-list: {exc}") from exc

    entries = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not entries:
        raise RuntimeError("--file-list 为空")

    abs_map: dict[str, FileNode] = {}
    rel_map: dict[str, FileNode] = {}
    for fn in files:
        abs_map[os.path.normcase(str(fn.path.resolve()))] = fn
        rel_map[os.path.normcase(fn.rel_path.as_posix())] = fn

    chosen: set[int] = set()
    missing: list[str] = []
    for entry in entries:
        p = Path(entry)
        if p.is_absolute():
            try:
                key = os.path.normcase(str(p.resolve()))
            except OSError:
                key = os.path.normcase(os.path.abspath(entry))
            fn = abs_map.get(key)
        else:
            key = os.path.normcase(str(p))
            fn = rel_map.get(key)
        if fn is None:
            missing.append(entry)
        else:
            chosen.add(id(fn))

    if missing:
        preview = "\n".join(f"  - {m}" for m in missing[:10])
        more = f"\n  ... 还有 {len(missing) - 10} 个" if len(missing) > 10 else ""
        raise RuntimeError(
            f"--file-list 中有 {len(missing)} 个路径不在扫描结果中：\n{preview}{more}"
        )

    result = [fn for fn in files if id(fn) in chosen]
    if not result:
        raise RuntimeError("--file-list 未匹配到任何可处理文件")
    return result


def _apply_duplicate_policy(files: list[FileNode], policy: str) -> None:
    action = _DUPLICATE_ACTIONS[policy]
    for f in files:
        if f.has_duplicate:
            f.duplicate_action = action


def _has_token(cfg: dict) -> bool:
    return bool(TokenManager.parse_tokens(cfg.get("token", "")))



def _cleanup_dir(d: Path) -> None:
    """删除临时目录，忽略错误。"""
    import shutil
    try:
        shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


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
    """用 rich.Tree 打印过滤后的目录结构，附带序号。"""
    idx_map: dict[int, FileNode] = {}  # 1-based序号 → FileNode
    counter = [0]

    tree: dict = {"dirs": {}, "files": []}
    for fn in all_files:
        node = tree
        for part in fn.rel_path.parts[:-1]:
            node = node["dirs"].setdefault(part, {"dirs": {}, "files": []})
        node["files"].append(fn)

    def _build_rich_tree(node: dict, rtree: RichTree) -> None:
        for name, child in node["dirs"].items():
            sub = rtree.add(f"[bold blue][D] {name}/[/bold blue]")
            _build_rich_tree(child, sub)
        for child in node["files"]:
            counter[0] += 1
            idx = counter[0]
            idx_map[idx] = child
            dup_tag = ""
            if child.has_duplicate:
                dup_tag = f" [yellow][!] [已有 {child.existing_md.name}][/yellow]"
            size_str = _fmt_size(child.size)
            rtree.add(
                f"[dim]{idx:>3}[/dim]  {child.path.name}{dup_tag}  [green]{size_str}[/green]"
            )

    root_tree = RichTree(f"[bold][D] {root.path.name or root.path}[/bold]")
    _build_rich_tree(tree, root_tree)
    console.print(root_tree)
    return idx_map


# ── 阶段二：文件选择 ──────────────────────────────────────────────────────────

def _phase_select(root: DirNode, all_files: list[FileNode] | None = None) -> list[FileNode]:
    """展示文件树，收集排除序号，返回已选文件列表。"""
    if all_files is None:
        all_files = flatten_files(root)
    if not all_files:
        console.print("[red]未找到任何支持的文件。[/red]")
        sys.exit(2)

    total_size = sum(f.size for f in all_files)
    dup_count = sum(1 for f in all_files if f.has_duplicate)

    console.rule("[bold cyan]MinerU 批量解析下载工具[/bold cyan]")
    console.print(
        f"[bold]扫描目录:[/bold] {root.path}\n"
        f"找到 [green]{len(all_files)}[/green] 个可解析文件"
        f"（共 [cyan]{_fmt_size(total_size)}[/cyan]）"
        + (f"  [yellow][!] {dup_count} 个已有 .md 输出[/yellow]" if dup_count else "")
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
        + (f"  [yellow][!] {dup_sel} 个有重复 .md[/yellow]" if dup_sel else "")
    )
    return selected


# ── 阶段三：重复文件处理 ──────────────────────────────────────────────────────

def _phase_duplicate(
    selected: list[FileNode],
    forced: str | None = None,
) -> list[FileNode]:
    """处理重复文件的决策，返回更新了 duplicate_action 的列表。"""
    dup_files = [f for f in selected if f.has_duplicate]
    if not dup_files:
        return selected

    if forced in _DUPLICATE_ACTIONS:
        _apply_duplicate_policy(selected, forced)
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

def _phase_options(cfg: dict, assume_yes: bool = False) -> dict:
    """展示当前配置，询问是否修改，返回最终生效配置。"""
    if assume_yes:
        return cfg

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
        self.file_output: dict[FileNode, str] = {}
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


def _handle_overlimit_files(
    nodes: list[FileNode],
    stem_map: dict,
    cfg: dict,
    token_manager,
    proxies: dict | None,
    batch_size: int,
    poll_interval: int,
    timeout: int,
    keep_zip: bool,
    keep_json: bool,
    proxy_mode: str,
    proxy_url: str,
    state: "_CliProgressState",
    progress,
    dl_task,
) -> None:
    """
    对每个超页 PDF：
    1. 用 pypdf 拆成 ≤180 页的分片（临时文件）
    2. 上传分片 → 轮询 → 下载各分片 .md
    3. 按顺序合并所有分片 .md 为最终 .md
    4. 清理临时分片
    """
    import tempfile
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        for fn in nodes:
            state.set(fn, "failed", "需要 pypdf 拆分但未安装，请 pip install pypdf")
            with state.lock:
                state.failed += 1
            state.log_line(f"[失败] {fn.path.name}: 需要 pypdf 拆分但未安装")
            progress.advance(dl_task)
        return

    from api import build_batches, submit_and_upload_batch, poll_batches_concurrent
    from processor import download_chunk_zip
    from dataclasses import dataclass

    CHUNK_PAGES = 180

    @dataclass
    class _TmpNode:
        path: Path

    for fn in nodes:
        state.log_line(f"[拆分] {fn.path.name}：读取页数...")
        state.set(fn, "parsing", "读取 PDF 页数...")
        try:
            reader = PdfReader(str(fn.path))
            total_pages = len(reader.pages)
        except Exception as exc:
            state.set(fn, "failed", f"读取 PDF 失败: {exc}")
            with state.lock:
                state.failed += 1
            state.log_line(f"[失败] {fn.path.name} 读取失败: {exc}")
            progress.advance(dl_task)
            continue

        state.log_line(f"[拆分] {fn.path.name}：{total_pages} 页，拆为 ≤{CHUNK_PAGES} 页的分片")
        state.set(fn, "parsing", f"共 {total_pages} 页，拆分中...")

        tmpdir = Path(tempfile.mkdtemp(prefix="mineru_split_"))
        chunk_paths: list[Path] = []
        try:
            chunk_idx = 0
            for start in range(0, total_pages, CHUNK_PAGES):
                end = min(start + CHUNK_PAGES, total_pages)
                writer = PdfWriter()
                for page_num in range(start, end):
                    writer.add_page(reader.pages[page_num])
                chunk_name = f"{fn.path.stem}_part{chunk_idx + 1:03d}.pdf"
                chunk_path = tmpdir / chunk_name
                with open(chunk_path, "wb") as f:
                    writer.write(f)
                chunk_paths.append(chunk_path)
                chunk_idx += 1
                state.set(fn, "parsing", f"已拆 {chunk_idx} 片 ({start + 1}-{end}/{total_pages})")
        except Exception as exc:
            state.set(fn, "failed", f"拆分失败: {exc}")
            with state.lock:
                state.failed += 1
            state.log_line(f"[失败] {fn.path.name} 拆分失败: {exc}")
            progress.advance(dl_task)
            _cleanup_dir(tmpdir)
            continue

        state.log_line(f"[拆分] {fn.path.name}：共 {len(chunk_paths)} 片，开始上传...")
        state.set(fn, "uploading", f"上传 {len(chunk_paths)} 个分片...")

        tmp_nodes = [_TmpNode(path=cp) for cp in chunk_paths]
        chunk_batches = build_batches(tmp_nodes, batch_size)
        chunk_batch_map: dict[str, list] = {}
        chunk_batch_token: dict[str, int] = {}

        upload_ok = True
        for cb in chunk_batches:
            try:
                bid, upload_res, tidx = submit_and_upload_batch(
                    cb, token_manager,
                    model_version="vlm",
                    language=cfg.get("language", "ch"),
                    proxies=proxies,
                    proxy_mode=proxy_mode,
                    proxy_url=proxy_url,
                )
                ok_nodes = [n for n in cb if upload_res.get(n.path, False)]
                chunk_batch_map[bid] = ok_nodes
                chunk_batch_token[bid] = tidx
            except Exception as exc:
                state.log_line(f"[失败] {fn.path.name} 分片上传失败: {exc}")
                upload_ok = False
                break

        if not upload_ok or not chunk_batch_map:
            state.set(fn, "failed", "分片上传失败")
            with state.lock:
                state.failed += 1
            state.log_line(f"[失败] {fn.path.name} 分片上传失败")
            progress.advance(dl_task)
            _cleanup_dir(tmpdir)
            continue

        state.set(fn, "parsing", f"轮询 {len(chunk_paths)} 个分片...")

        chunk_done: set[str] = set()
        chunk_failed: set[str] = set()
        chunk_total = sum(len(v) for v in chunk_batch_map.values())

        def _on_chunk_progress(bid: str, results: list[dict]) -> None:
            for r in results:
                cname = r.get("file_name", "")
                s = r.get("state", "")
                if s == "done":
                    chunk_done.add(cname)
                elif s == "failed":
                    chunk_failed.add(cname)
            done_n = len(chunk_done)
            fail_n = len(chunk_failed)
            running = [r for r in results if r.get("state") == "running"]
            if running:
                prog = running[0].get("extract_progress", {})
                pg = f"{prog.get('extracted_pages', '?')}/{prog.get('total_pages', '?')}页"
                state.set(fn, "parsing",
                          f"解析分片 {done_n + fail_n}/{chunk_total}，当前: {pg}，完成 {done_n} 失败 {fail_n}")
            else:
                state.set(fn, "parsing",
                          f"解析分片 {done_n + fail_n}/{chunk_total}，完成 {done_n} 失败 {fail_n}")

        chunk_pairs = [(bid, chunk_batch_token[bid]) for bid in chunk_batch_map]
        chunk_poll = poll_batches_concurrent(
            chunk_pairs, token_manager,
            interval=poll_interval, timeout=timeout, proxies=proxies,
            on_batch_progress=_on_chunk_progress,
        )

        state.set(fn, "downloading", "下载并合并分片...")

        part_mds: dict[str, str] = {}
        download_fail = False
        for bid, results in chunk_poll.items():
            ok_nodes = chunk_batch_map.get(bid, [])
            cname_map = {n.path.name: n for n in ok_nodes}
            for r in results:
                cname = r.get("file_name", "")
                cnode = cname_map.get(cname)
                if cnode is None:
                    continue
                if r.get("state") != "done":
                    state.log_line(f"[警告] 分片 {cname} 解析未完成: {r.get('err_msg', '')}")
                    download_fail = True
                    continue
                zip_url = r.get("full_zip_url", "")
                if not zip_url:
                    download_fail = True
                    continue
                chunk_stem = cnode.path.stem
                md_text = download_chunk_zip(
                    zip_url,
                    out_dir=fn.path.parent,
                    chunk_stem=chunk_stem,
                    keep_zip=keep_zip,
                    keep_json=keep_json,
                    proxies=proxies,
                )
                if md_text is None:
                    download_fail = True
                    state.log_line(f"[警告] 分片 {cname} 下载失败")
                    continue
                part_mds[cname] = md_text

        if download_fail or len(part_mds) < len(chunk_paths):
            state.log_line(f"[警告] {fn.path.name} 部分分片失败，尝试合并已有部分")

        ordered_mds = []
        for cp in chunk_paths:
            md = part_mds.get(cp.name)
            if md:
                ordered_mds.append(md)

        if not ordered_mds:
            state.set(fn, "failed", "所有分片下载均失败")
            with state.lock:
                state.failed += 1
            state.log_line(f"[失败] {fn.path.name} 所有分片均失败")
            progress.advance(dl_task)
            _cleanup_dir(tmpdir)
            continue

        out_stem = stem_map.get(fn) or fn.path.stem
        out_md = fn.path.parent / f"{out_stem}.md"
        combined = ""
        for i, md in enumerate(ordered_mds):
            if i > 0:
                combined += f"\n\n---\n<!-- 第 {i + 1} 部分 -->\n\n"
            combined += md
        try:
            out_md.write_text(combined, encoding="utf-8")
            state.set(fn, "done", f"✓ 拆分合并 → {out_md.name}")
            state.file_output[fn] = str(out_md)
            with state.lock:
                state.done += 1
            state.log_line(
                f"[完成] {fn.path.name} 拆分合并完成 → {out_md.name}"
                f"（{len(ordered_mds)}/{len(chunk_paths)} 片）"
            )
        except OSError as exc:
            state.set(fn, "failed", f"写入合并 .md 失败: {exc}")
            with state.lock:
                state.failed += 1
            state.log_line(f"[失败] {fn.path.name} 写入合并 .md 失败: {exc}")
        progress.advance(dl_task)

        _cleanup_dir(tmpdir)


def _phase_process(
    selected: list[FileNode],
    cfg: dict,
    assume_yes: bool = False,
    quiet: bool = False,
    root: Path | None = None,
) -> dict:
    """提交、轮询、下载，展示进度并返回结构化汇总。"""
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
    token_manager = TokenManager(tokens, lb_enabled=bool(lb)) if tokens else None
    if not quiet and to_process and token_manager is not None:
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
        disable=quiet or not to_process,
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
                    if "number of pages exceeds limit" in r.get("err_msg", ""):
                        state.set(fn, "parsing", "超页，准备自动拆分...")
                    else:
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

        # ── 检测超页失败，自动拆分重提 ──
        overlimit_nodes: list[FileNode] = []
        for bid, extract_results in poll_results.items():
            nodes = batch_node_map.get(bid, [])
            name_map = {fn.path.name: fn for fn in nodes}
            for r in extract_results:
                fn = name_map.get(r.get("file_name", ""))
                if fn is None:
                    continue
                if r.get("state") == "failed" and "number of pages exceeds limit" in r.get("err_msg", ""):
                    overlimit_nodes.append(fn)

        if overlimit_nodes:
            state.log_line(f"发现 {len(overlimit_nodes)} 个超页文件，尝试自动拆分重提...")
            for fn in overlimit_nodes:
                state.set(fn, "parsing", "拆分中...")
            _handle_overlimit_files(
                overlimit_nodes, stem_map, cfg, token_manager, proxies,
                batch_size, poll_interval, timeout, keep_zip, keep_json,
                proxy_mode, proxy_url, state, progress, dl_task,
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
                    state.file_output[fn] = str(md_path)
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
    for fn in to_process:
        if state.file_status.get(fn) not in ("done", "failed"):
            state.set(fn, "failed", "未完成（轮询超时或未返回结果）")

    files_payload: list[dict] = []
    for fn in selected:
        if fn.duplicate_action == DuplicateAction.SKIP:
            status = "skipped"
            msg = "已有同名 .md，按策略跳过"
            output = None
        else:
            status = "done" if state.file_status.get(fn) == "done" else "failed"
            msg = state.file_msg.get(fn, "")
            output = state.file_output.get(fn)
        files_payload.append({
            "path": str(fn.path),
            "rel_path": str(fn.rel_path),
            "size": fn.size,
            "status": status,
            "message": msg,
            "output": output,
        })

    done_n = sum(1 for f in files_payload if f["status"] == "done")
    failed_n = sum(1 for f in files_payload if f["status"] == "failed")
    skipped_n = sum(1 for f in files_payload if f["status"] == "skipped")
    batches = build_batches(to_process, batch_size)
    result = {
        "success": failed_n == 0,
        "root": str(root) if root is not None else None,
        "config": {
            "token_configured": bool(tokens),
            "language": language,
            "batch_size": batch_size,
            "poll_interval": poll_interval,
            "timeout": timeout,
            "keep_zip": bool(keep_zip),
            "keep_json": bool(keep_json),
            "proxy_mode": proxy_mode,
            "lb_enabled": bool(lb),
        },
        "summary": {
            "total": len(selected),
            "done": done_n,
            "failed": failed_n,
            "skipped": skipped_n,
            "batches": len(batches),
            "selected_size": sum(f.size for f in to_process),
        },
        "files": files_payload,
    }

    if not quiet:
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
                result_table.add_row("[green]OK[/green]", str(fn.rel_path), f"[green]{msg}[/green]")
            else:
                result_table.add_row("[red]FAIL[/red]", str(fn.rel_path), f"[red]{msg}[/red]")

        for fn in skipped:
            result_table.add_row("[dim]—[/dim]", str(fn.rel_path), "[dim]已跳过（保留旧 .md）[/dim]")

        console.print(result_table)
        console.print(
            f"\n[bold]成功: [green]{done_n}[/green]  "
            f"失败: [red]{failed_n}[/red]  "
            f"跳过: [yellow]{skipped_n}[/yellow][/bold]"
        )
        if failed_n:
            console.print("[dim]详细错误已写入 mineru_errors.log[/dim]")

        if not assume_yes:
            _ask("\n按回车退出: ")

    return result


# ── 主入口 ────────────────────────────────────────────────────────────────────

def _build_dry_run_payload(
    root: Path,
    files: list[FileNode],
    cfg: dict,
    policy: str,
) -> dict:
    to_process = [f for f in files if f.duplicate_action != DuplicateAction.SKIP]
    batch_size = int(cfg.get("batch_size", 20))
    return {
        "success": True,
        "dry_run": True,
        "root": str(root),
        "config": {
            "token_configured": _has_token(cfg),
            "language": cfg.get("language", "ch"),
            "batch_size": batch_size,
            "poll_interval": int(cfg.get("poll_interval", 8)),
            "timeout": int(cfg.get("timeout", 1800)),
            "keep_zip": bool(cfg.get("keep_zip", False)),
            "keep_json": bool(cfg.get("keep_json", False)),
            "proxy_mode": cfg.get("proxy_mode", "system"),
            "lb_enabled": cfg.get("lb_enabled"),
            "duplicate_policy": policy,
        },
        "summary": {
            "total": len(files),
            "will_process": len(to_process),
            "skipped": len(files) - len(to_process),
            "batches": (len(to_process) + batch_size - 1) // batch_size,
            "selected_size": sum(f.size for f in to_process),
        },
        "files": [
            {
                "index": i,
                "path": str(f.path),
                "rel_path": str(f.rel_path),
                "size": f.size,
                "existing_md": str(f.existing_md) if f.existing_md else None,
                "duplicate_action": f.duplicate_action.value,
                "will_process": f.duplicate_action != DuplicateAction.SKIP,
            }
            for i, f in enumerate(files, 1)
        ],
    }


def _print_dry_run(payload: dict) -> None:
    console.rule("[bold cyan]Dry run（不会提交任何文件）[/bold cyan]")
    console.print(f"[bold]扫描目录:[/bold] {payload['root']}")
    console.print(
        f"共 [green]{payload['summary']['total']}[/green] 个文件，"
        f"将处理 [green]{payload['summary']['will_process']}[/green] 个，"
        f"跳过 [yellow]{payload['summary']['skipped']}[/yellow] 个"
        f"（{_fmt_size(payload['summary']['selected_size'])}，"
        f"{payload['summary']['batches']} 批）"
    )
    table = Table(show_header=True, header_style="bold")
    table.add_column("#", width=4, justify="right")
    table.add_column("文件", style="cyan")
    table.add_column("大小", justify="right")
    table.add_column("重复 .md", justify="center")
    table.add_column("处理", justify="center")
    for f in payload["files"]:
        table.add_row(
            str(f["index"]),
            f["rel_path"],
            _fmt_size(f["size"]),
            "是" if f["existing_md"] else "否",
            "处理" if f["will_process"] else "[yellow]跳过[/yellow]",
        )
    console.print(table)
    console.print(
        f"[dim]Token: {'已配置' if payload['config']['token_configured'] else '未配置'}"
        f"  语言: {payload['config']['language']}"
        f"  重复策略: {payload['config']['duplicate_policy']}[/dim]"
    )


def run_cli(
    root_node: DirNode,
    cfg: dict,
    *,
    interactive: bool = False,
    dry_run: bool = False,
    json_output: bool = False,
    duplicate: str | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    file_list: str | None = None,
) -> int:
    """CLI 模式入口。

    默认是一次性非交互执行；interactive=True 时进入旧版向导。
    返回进程退出码：0 成功，1 有文件失败，2 配置/扫描错误。
    """
    all_files = _filter_files(flatten_files(root_node), include, exclude)

    if file_list is not None:
        try:
            all_files = _load_file_list(Path(file_list), all_files)
        except RuntimeError as exc:
            msg = str(exc)
            if json_output:
                _print_json({
                    "success": False,
                    "root": str(root_node.path),
                    "error": msg,
                    "files": [],
                })
            else:
                console.print(msg, style="red")
            return 2

    if not all_files:
        msg = "未找到匹配的文件（检查 --include / --exclude / --file-list）"
        if json_output:
            _print_json({
                "success": False,
                "root": str(root_node.path),
                "error": msg,
                "files": [],
            })
        else:
            console.print(msg, style="red")
        return 2

    policy = _duplicate_policy(duplicate, cfg)

    if dry_run:
        _apply_duplicate_policy(all_files, policy)
        payload = _build_dry_run_payload(root_node.path, all_files, cfg, policy)
        if json_output:
            _print_json(payload)
        else:
            _print_dry_run(payload)
        return 0

    if interactive:
        selected = _phase_select(root_node, all_files)
        selected = _phase_duplicate(selected, duplicate)
        cfg = _phase_options(cfg)
        if not _phase_confirm(selected, cfg):
            # 退回文件选择
            console.print("[yellow]已取消，重新选择文件…[/yellow]")
            return run_cli(
                root_node,
                cfg,
                interactive=True,
                dry_run=dry_run,
                json_output=json_output,
                duplicate=duplicate,
                include=include,
                exclude=exclude,
                file_list=file_list,
            )
        result = _phase_process(selected, cfg, root=root_node.path)
        return 0 if result["success"] else 1

    # 一次性非交互执行
    _apply_duplicate_policy(all_files, policy)
    to_process = [f for f in all_files if f.duplicate_action != DuplicateAction.SKIP]
    if to_process and not _has_token(cfg):
        msg = "未配置 MinerU Token（设置 MINERU_TOKEN 或使用 --token）"
        if json_output:
            _print_json({
                "success": False,
                "root": str(root_node.path),
                "error": msg,
                "files": [],
            })
        else:
            console.print(msg, style="red")
        return 2
    if not json_output:
        console.print(
            f"[bold]扫描目录:[/bold] {root_node.path}\n"
            f"共 [green]{len(all_files)}[/green] 个文件，"
            f"将处理 [green]{len(to_process)}[/green] 个，"
            f"跳过 [yellow]{len(all_files) - len(to_process)}[/yellow] 个"
            f"（重复 .md 策略: {policy}）"
        )
    result = _phase_process(
        all_files, cfg, assume_yes=True, quiet=json_output, root=root_node.path
    )
    if json_output:
        _print_json(result)
    return 0 if result["success"] else 1
