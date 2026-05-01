"""
tui.py — Textual TUI 应用。
包含两个主屏：
  SelectScreen  — 文件树选择 + 选项面板
  ProgressScreen — 处理进度展示
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Select,
    Static,
)
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from scanner import DirNode, DuplicateAction, FileNode, flatten_files, resolve_output_stem, selected_files
from config import load_config, save_config
from token_manager import TokenManager

logger = logging.getLogger(__name__)

# ── CSS ───────────────────────────────────────────────────────────────────────
CSS = """
Screen {
    background: $surface;
}

#main-layout {
    height: 1fr;
    layout: horizontal;
}

#file-panel {
    width: 2fr;
    border: solid $primary;
    padding: 0 1;
}

#options-panel {
    width: 1fr;
    border: solid $accent;
    padding: 0 1;
    overflow-y: auto;
}

#options-panel Label {
    margin-top: 1;
    color: $text-muted;
}

#file-tree {
    height: 1fr;
}

#status-bar {
    height: 3;
    background: $boost;
    padding: 0 1;
    layout: horizontal;
    align: left middle;
}

#status-text {
    width: 1fr;
}

#warn-text {
    color: $warning;
}

.dup-row {
    color: $warning;
}

.dup-actions {
    padding-left: 4;
    height: 1;
}

/* Progress screen */
#progress-layout {
    height: 1fr;
    layout: horizontal;
}

#progress-list {
    width: 2fr;
    border: solid $primary;
    padding: 0 1;
    overflow-y: auto;
}

#progress-info {
    width: 1fr;
    border: solid $accent;
    padding: 0 1;
    overflow-y: auto;
}

.status-done   { color: $success; }
.status-fail   { color: $error; }
.status-run    { color: $warning; }
.status-wait   { color: $text-muted; }

#log-panel {
    height: 12;
    border: solid $panel;
    overflow-y: auto;
    padding: 0 1;
}

#summary-panel {
    padding: 1;
}
"""

# ── 辅助：文件大小格式化 ──────────────────────────────────────────────────────

def _fmt_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}" if unit != "B" else f"{b} B"
        b /= 1024
    return f"{b:.1f} TB"


# ══════════════════════════════════════════════════════════════════════════════
# SelectScreen
# ══════════════════════════════════════════════════════════════════════════════

class SelectScreen(Screen):
    """文件选择 + 选项配置界面。"""

    BINDINGS = [
        Binding("a", "select_all", "全选"),
        Binding("n", "deselect_all", "全不选"),
        Binding("t", "toggle_item", "切换选中"),
        Binding("d", "handle_dup", "处理重复", show=False),
        Binding("g", "confirm", "开始解析"),
        Binding("q,escape", "quit_app", "退出"),
    ]

    def __init__(self, root_node: DirNode, cfg: dict, **kwargs):
        super().__init__(**kwargs)
        self._root_node = root_node
        self._cfg = dict(cfg)
        # node_map: tree node id → FileNode or DirNode
        self._node_map: dict[int, FileNode | DirNode] = {}
        # 反向映射：FileNode → TreeNode，用于原地更新标签
        self._fn_to_tree: dict[FileNode, TreeNode] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="main-layout"):
            with Vertical(id="file-panel"):
                yield Label("📁 文件列表  T 切换选中 · D 处理重复 · G 开始解析")
                yield Tree("根目录", id="file-tree")
            with ScrollableContainer(id="options-panel"):
                yield Label("⚙ 解析选项")
                yield Label("MinerU Token（多个用逗号分隔）")
                raw_token = self._cfg.get("token", "")
                if isinstance(raw_token, list):
                    raw_token = ",".join(raw_token)
                yield Input(
                    value=raw_token,
                    password=True,
                    placeholder="sk-...（多个 Token 用逗号分隔）",
                    id="input-token",
                )
                yield Checkbox(
                    "启用负载均衡（多 Token 轮询）",
                    value=bool(self._cfg.get("lb_enabled", False)),
                    id="cb-lb",
                )
                yield Label("文档语言")
                lang_options = [
                    ("ch — 中英文（默认）", "ch"),
                    ("en — 纯英文", "en"),
                    ("japan — 日文", "japan"),
                    ("korean — 韩文", "korean"),
                    ("latin — 拉丁语系", "latin"),
                ]
                yield Select(
                    lang_options,
                    value=self._cfg.get("language", "ch"),
                    id="select-lang",
                )
                yield Label("输出选项")
                yield Checkbox(
                    "保留 ZIP 压缩包",
                    value=self._cfg.get("keep_zip", False),
                    id="cb-keep-zip",
                )
                yield Checkbox(
                    "保留 JSON 文件",
                    value=self._cfg.get("keep_json", False),
                    id="cb-keep-json",
                )
                yield Label("代理设置")
                with RadioSet(id="proxy-set"):
                    yield RadioButton(
                        "使用系统代理",
                        value=self._cfg.get("proxy_mode", "system") == "system",
                        id="proxy-system",
                    )
                    yield RadioButton(
                        "自定义代理",
                        value=self._cfg.get("proxy_mode") == "custom",
                        id="proxy-custom",
                    )
                    yield RadioButton(
                        "不使用代理",
                        value=self._cfg.get("proxy_mode") == "none",
                        id="proxy-none",
                    )
                yield Input(
                    value=self._cfg.get("proxy_url", ""),
                    placeholder="http://127.0.0.1:7890",
                    id="input-proxy-url",
                )
                yield Button("保存配置", id="btn-save-cfg", variant="default")
                yield Button("▶ 开始解析", id="btn-confirm", variant="success")

        with Horizontal(id="status-bar"):
            yield Static("", id="status-text")
            yield Static("", id="warn-text")
        yield Footer()

    def on_mount(self) -> None:
        self._build_tree()
        self._refresh_status()

    # ── 树构建 ────────────────────────────────────────────────────────────────

    def _build_tree(self) -> None:
        tree: Tree = self.query_one("#file-tree", Tree)
        tree.clear()
        self._node_map.clear()
        self._fn_to_tree.clear()
        root = tree.root
        root.label = str(self._root_node.rel_path) or "."
        root.expand()
        self._populate_node(root, self._root_node)

    def _populate_node(self, tree_node: TreeNode, dir_node: DirNode) -> None:
        for child in dir_node.children:
            if isinstance(child, DirNode):
                label = f"📁 {child.path.name}/"
                sub = tree_node.add(label, expand=True)
                self._node_map[id(sub)] = child
                self._populate_node(sub, child)
            else:
                label = self._file_label(child)
                leaf = tree_node.add_leaf(label)
                self._node_map[id(leaf)] = child
                self._fn_to_tree[child] = leaf

    def _file_label(self, fn: FileNode) -> str:
        check = "☑" if fn.selected else "☐"
        warn = " ⚠" if fn.has_duplicate else ""
        dup_info = ""
        if fn.has_duplicate:
            action_map = {
                DuplicateAction.NONE: "[未决定]",
                DuplicateAction.OVERWRITE: "[覆盖]",
                DuplicateAction.SKIP: "[跳过]",
                DuplicateAction.RENAME: "[改名]",
            }
            dup_info = f" {action_map[fn.duplicate_action]}"
        size = _fmt_size(fn.size)
        return f"{check}{warn} {fn.path.name}{dup_info}  {size}"

    def _refresh_tree_labels(self) -> None:
        # 原地更新每个文件叶节点的 label，不重建树（避免目录展开状态被重置）
        for fn, tree_node in self._fn_to_tree.items():
            tree_node.set_label(self._file_label(fn))

    # ── 状态栏 ────────────────────────────────────────────────────────────────

    def _refresh_status(self) -> None:
        all_files = flatten_files(self._root_node)
        sel = [f for f in all_files if f.selected]
        total_size = sum(f.size for f in sel)
        dup_undecided = [
            f for f in sel
            if f.has_duplicate and f.duplicate_action == DuplicateAction.NONE
        ]

        self.query_one("#status-text", Static).update(
            f"已选 {len(sel)} 个文件，共 {_fmt_size(total_size)}"
        )
        if dup_undecided:
            self.query_one("#warn-text", Static).update(
                f"  ⚠ {len(dup_undecided)} 个文件有已存在的 .md 未处理（按 D 设置）"
            )
        else:
            self.query_one("#warn-text", Static).update("")

    # ── 按键动作 ──────────────────────────────────────────────────────────────

    def action_select_all(self) -> None:
        for fn in flatten_files(self._root_node):
            fn.selected = True
        self._refresh_tree_labels()
        self._refresh_status()

    def action_deselect_all(self) -> None:
        for fn in flatten_files(self._root_node):
            fn.selected = False
        self._refresh_tree_labels()
        self._refresh_status()

    def action_toggle_item(self) -> None:
        tree: Tree = self.query_one("#file-tree", Tree)
        cursor = tree.cursor_node
        if cursor is None:
            return
        obj = self._node_map.get(id(cursor))
        if obj is None:
            return
        if isinstance(obj, FileNode):
            obj.selected = not obj.selected
        else:
            # 目录：切换其下所有文件
            files = flatten_files(obj)
            new_state = not all(f.selected for f in files)
            for f in files:
                f.selected = new_state
        self._refresh_tree_labels()
        self._refresh_status()

    def action_handle_dup(self) -> None:
        """弹出重复文件处理对话框。"""
        tree: Tree = self.query_one("#file-tree", Tree)
        cursor = tree.cursor_node
        if cursor is None:
            return
        obj = self._node_map.get(id(cursor))
        if not isinstance(obj, FileNode) or not obj.has_duplicate:
            return
        self.app.push_screen(DupActionScreen(obj), self._on_dup_decided)

    def _on_dup_decided(self, _result: Any) -> None:
        self._refresh_tree_labels()
        self._refresh_status()

    def action_confirm(self) -> None:
        all_files = flatten_files(self._root_node)
        sel = [f for f in all_files if f.selected]
        if not sel:
            self.notify("没有选中任何文件", severity="warning")
            return

        # 检查是否有未决定的重复文件
        undecided = [f for f in sel if f.has_duplicate and f.duplicate_action == DuplicateAction.NONE]
        if undecided:
            self.app.push_screen(
                BatchDupScreen(undecided),
                self._on_batch_dup_decided,
            )
            return

        self._do_confirm(sel)

    def _on_batch_dup_decided(self, action: DuplicateAction | None) -> None:
        if action is None:
            return  # 用户取消
        sel = [f for f in flatten_files(self._root_node) if f.selected]
        for f in sel:
            if f.has_duplicate and f.duplicate_action == DuplicateAction.NONE:
                f.duplicate_action = action
        self._refresh_tree_labels()
        self._do_confirm(sel)

    def _do_confirm(self, sel: list[FileNode]) -> None:
        # 收集配置
        try:
            self._cfg["token"] = self.query_one("#input-token", Input).value.strip()
            self._cfg["lb_enabled"] = self.query_one("#cb-lb", Checkbox).value
            lang_widget = self.query_one("#select-lang", Select)
            if lang_widget.value != Select.BLANK:
                self._cfg["language"] = lang_widget.value
            self._cfg["keep_zip"] = self.query_one("#cb-keep-zip", Checkbox).value
            self._cfg["keep_json"] = self.query_one("#cb-keep-json", Checkbox).value
            proxy_url = self.query_one("#input-proxy-url", Input).value.strip()
            self._cfg["proxy_url"] = proxy_url
        except NoMatches:
            pass

        # 检查 Token
        tokens = TokenManager.parse_tokens(self._cfg.get("token", ""))
        if not tokens:
            self.notify("请填写 MinerU Token", severity="error")
            return

        # 构建 TokenManager
        lb = self._cfg.get("lb_enabled")
        if lb is None:
            lb = len(tokens) > 1
        token_manager = TokenManager(tokens, lb_enabled=bool(lb))

        # 过渡到进度屏
        to_process = [f for f in sel if f.duplicate_action != DuplicateAction.SKIP]
        self.app.push_screen(ProgressScreen(to_process, self._cfg, token_manager))

    def action_quit_app(self) -> None:
        self.app.exit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirm":
            self.action_confirm()
            return
        if event.button.id == "btn-save-cfg":
            try:
                self._cfg["token"] = self.query_one("#input-token", Input).value.strip()
                self._cfg["lb_enabled"] = self.query_one("#cb-lb", Checkbox).value
                lang_widget = self.query_one("#select-lang", Select)
                if lang_widget.value != Select.BLANK:
                    self._cfg["language"] = lang_widget.value
                self._cfg["keep_zip"] = self.query_one("#cb-keep-zip", Checkbox).value
                self._cfg["keep_json"] = self.query_one("#cb-keep-json", Checkbox).value
                proxy_url = self.query_one("#input-proxy-url", Input).value.strip()
                self._cfg["proxy_url"] = proxy_url
            except NoMatches:
                pass
            save_config(self._cfg)
            self.notify("配置已保存到 mineru_config.yaml", severity="information")

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id == "proxy-set":
            idx = event.index
            modes = ["system", "custom", "none"]
            if idx < len(modes):
                self._cfg["proxy_mode"] = modes[idx]


# ══════════════════════════════════════════════════════════════════════════════
# DupActionScreen — 单个文件重复处理弹窗
# ══════════════════════════════════════════════════════════════════════════════

class DupActionScreen(Screen):
    """处理单个文件的重复 .md 选项弹窗。"""

    BINDINGS = [
        Binding("o", "set_overwrite", "覆盖"),
        Binding("s", "set_skip", "跳过"),
        Binding("r", "set_rename", "改名"),
        Binding("escape", "cancel", "取消"),
    ]

    def __init__(self, fn: FileNode, **kwargs):
        super().__init__(**kwargs)
        self._fn = fn

    def compose(self) -> ComposeResult:
        with Container():
            yield Label(f"文件：{self._fn.rel_path}")
            yield Label(f"已有：{self._fn.existing_md}")
            yield Label("")
            yield Button("(O) 覆盖 — 删除旧 .md，重新解析", id="btn-overwrite", variant="warning")
            yield Button("(S) 跳过 — 保留旧 .md，不提交解析", id="btn-skip", variant="default")
            yield Button("(R) 改名 — 输出为 {stem}_1.md", id="btn-rename", variant="primary")
            yield Button("取消", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action_map = {
            "btn-overwrite": DuplicateAction.OVERWRITE,
            "btn-skip": DuplicateAction.SKIP,
            "btn-rename": DuplicateAction.RENAME,
        }
        if event.button.id in action_map:
            self._fn.duplicate_action = action_map[event.button.id]
            self.dismiss(self._fn.duplicate_action)
        else:
            self.dismiss(None)

    def action_set_overwrite(self): self._fn.duplicate_action = DuplicateAction.OVERWRITE; self.dismiss(DuplicateAction.OVERWRITE)
    def action_set_skip(self): self._fn.duplicate_action = DuplicateAction.SKIP; self.dismiss(DuplicateAction.SKIP)
    def action_set_rename(self): self._fn.duplicate_action = DuplicateAction.RENAME; self.dismiss(DuplicateAction.RENAME)
    def action_cancel(self): self.dismiss(None)


# ══════════════════════════════════════════════════════════════════════════════
# BatchDupScreen — 批量设置重复文件的默认处理
# ══════════════════════════════════════════════════════════════════════════════

class BatchDupScreen(Screen):
    """批量设置所有未决定重复文件的处理方式。"""

    def __init__(self, undecided: list[FileNode], **kwargs):
        super().__init__(**kwargs)
        self._undecided = undecided

    def compose(self) -> ComposeResult:
        with Container():
            yield Label(f"有 {len(self._undecided)} 个文件存在重复 .md，请选择统一处理方式：")
            yield Button("(A) 全部覆盖", id="btn-all-overwrite", variant="warning")
            yield Button("(K) 全部跳过", id="btn-all-skip", variant="default")
            yield Button("(N) 全部改名", id="btn-all-rename", variant="primary")
            yield Button("取消", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action_map = {
            "btn-all-overwrite": DuplicateAction.OVERWRITE,
            "btn-all-skip": DuplicateAction.SKIP,
            "btn-all-rename": DuplicateAction.RENAME,
        }
        if event.button.id in action_map:
            self.dismiss(action_map[event.button.id])
        else:
            self.dismiss(None)


# ══════════════════════════════════════════════════════════════════════════════
# ProgressScreen
# ══════════════════════════════════════════════════════════════════════════════

# 文件处理状态
class FileStatus:
    WAITING = "waiting"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    PARSING = "parsing"
    DOWNLOADING = "downloading"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


_STATUS_ICON = {
    FileStatus.WAITING: "…",
    FileStatus.UPLOADING: "↑",
    FileStatus.UPLOADED: "↑✓",
    FileStatus.PARSING: "⠿",
    FileStatus.DOWNLOADING: "↓",
    FileStatus.DONE: "✓",
    FileStatus.FAILED: "✗",
    FileStatus.SKIPPED: "—",
}

_STATUS_CSS = {
    FileStatus.DONE: "status-done",
    FileStatus.FAILED: "status-fail",
    FileStatus.PARSING: "status-run",
    FileStatus.DOWNLOADING: "status-run",
    FileStatus.WAITING: "status-wait",
}


class ProgressScreen(Screen):
    """处理进度界面。"""

    BINDINGS = [
        Binding("q,escape", "quit_app", "退出"),
    ]

    # 响应式变量
    _log_lines: reactive[list[str]] = reactive(list)

    def __init__(self, file_nodes: list[FileNode], cfg: dict, token_manager: TokenManager, **kwargs):
        super().__init__(**kwargs)
        self._file_nodes = file_nodes
        self._cfg = cfg
        self._token_manager = token_manager
        self._statuses: dict[FileNode, str] = {fn: FileStatus.WAITING for fn in file_nodes}
        self._messages: dict[FileNode, str] = {}
        self._log: list[str] = []
        self._start_time = time.monotonic()
        self._done_count = 0
        self._fail_count = 0
        self._total = len(file_nodes)
        self._finished = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="progress-layout"):
            with ScrollableContainer(id="progress-list"):
                yield Label("处理进度")
                for fn in self._file_nodes:
                    yield Static(
                        self._file_line(fn),
                        id=f"fl-{id(fn)}",
                    )
            with Vertical(id="progress-info"):
                yield Static("", id="summary-panel")
                yield Label("实时日志")
                with ScrollableContainer(id="log-panel"):
                    yield Static("", id="log-content")
        yield Footer()

    def on_mount(self) -> None:
        # 在后台线程中运行处理逻辑
        self._worker_thread = threading.Thread(target=self._run_processing, daemon=True)
        self._worker_thread.start()
        # 定时刷新 UI
        self.set_interval(1.0, self._refresh_ui)

    # ── UI 刷新 ───────────────────────────────────────────────────────────────

    def _file_line(self, fn: FileNode) -> str:
        status = self._statuses.get(fn, FileStatus.WAITING)
        icon = _STATUS_ICON.get(status, "?")
        msg = self._messages.get(fn, "")
        return f"[{icon}] {fn.rel_path}  {msg}"

    def _refresh_ui(self) -> None:
        # 更新每个文件行
        for fn in self._file_nodes:
            widget_id = f"#fl-{id(fn)}"
            try:
                w = self.query_one(widget_id, Static)
                w.update(self._file_line(fn))
                status = self._statuses.get(fn)
                css_class = _STATUS_CSS.get(status, "")
                w.set_class(status == FileStatus.DONE, "status-done")
                w.set_class(status == FileStatus.FAILED, "status-fail")
                w.set_class(status in (FileStatus.PARSING, FileStatus.DOWNLOADING), "status-run")
                w.set_class(status == FileStatus.WAITING, "status-wait")
            except NoMatches:
                pass

        # 更新摘要
        elapsed = int(time.monotonic() - self._start_time)
        mm, ss = divmod(elapsed, 60)
        summary = (
            f"总进度: {self._done_count + self._fail_count} / {self._total}\n"
            f"✓ 成功: {self._done_count}  ✗ 失败: {self._fail_count}\n"
            f"已耗时: {mm:02d}:{ss:02d}\n"
        )
        # Token 状态
        summary += "\nToken 状态:\n" + "\n".join(self._token_manager.status_lines())
        if self._finished:
            summary += "\n\n[已完成] 按 Q 退出"
        try:
            self.query_one("#summary-panel", Static).update(summary)
        except NoMatches:
            pass

        # 更新日志
        log_text = "\n".join(self._log[-20:])
        try:
            self.query_one("#log-content", Static).update(log_text)
        except NoMatches:
            pass

    # ── 处理线程 ──────────────────────────────────────────────────────────────

    def _run_processing(self) -> None:
        """在后台线程中执行完整的上传 → 轮询 → 下载流程。"""
        from api import build_batches, submit_and_upload_batch, poll_batches_concurrent
        from processor import download_results_concurrent
        from scanner import DuplicateAction, resolve_output_stem
        import logging as _logging

        cfg = self._cfg
        token_manager = self._token_manager
        proxies = __import__("config").build_proxies(cfg["proxy_mode"], cfg.get("proxy_url", ""))
        proxy_mode = cfg["proxy_mode"]
        proxy_url = cfg.get("proxy_url", "")
        batch_size = int(cfg.get("batch_size", 20))
        poll_interval = int(cfg.get("poll_interval", 8))
        timeout = int(cfg.get("timeout", 1800))
        keep_zip = cfg.get("keep_zip", False)
        keep_json = cfg.get("keep_json", False)

        # 过滤掉 SKIP 文件
        to_process = [fn for fn in self._file_nodes if fn.duplicate_action != DuplicateAction.SKIP]
        skipped = [fn for fn in self._file_nodes if fn.duplicate_action == DuplicateAction.SKIP]
        for fn in skipped:
            self._statuses[fn] = FileStatus.SKIPPED
            self._messages[fn] = "已跳过（保留旧 .md）"

        # 预处理：OVERWRITE 时删除旧 .md
        for fn in to_process:
            if fn.duplicate_action == DuplicateAction.OVERWRITE and fn.existing_md:
                try:
                    fn.existing_md.unlink(missing_ok=True)
                except OSError as e:
                    self._log_line(f"[警告] 无法删除旧 .md {fn.existing_md}: {e}")

        batches = build_batches(to_process, batch_size)
        self._log_line(f"共 {len(to_process)} 个文件，分 {len(batches)} 批处理")

        # batch_id → [FileNode]
        batch_node_map: dict[str, list[FileNode]] = {}
        # batch_id → token_idx
        batch_token_idx: dict[str, int] = {}
        # FileNode → output_stem
        stem_map: dict[FileNode, str] = {}

        # ── 上传阶段（逐批） ──
        for i, batch in enumerate(batches):
            self._log_line(f"第 {i+1}/{len(batches)} 批：上传 {len(batch)} 个文件")
            for fn in batch:
                self._statuses[fn] = FileStatus.UPLOADING
                stem_map[fn] = resolve_output_stem(fn)

            def _on_uploaded(path: Path, ok: bool):
                fn_match = next((f for f in batch if f.path == path), None)
                if fn_match:
                    if ok:
                        self._statuses[fn_match] = FileStatus.UPLOADED
                        self._messages[fn_match] = "上传完成，等待解析"
                    else:
                        self._statuses[fn_match] = FileStatus.FAILED
                        self._messages[fn_match] = "上传失败"
                        self._fail_count += 1
                        self._log_line(f"[失败] 上传 {path.name}")
                        _logging.getLogger("app").error(
                            "file=%s stage=upload msg=上传失败", path
                        )

            try:
                batch_id, upload_results, token_idx = submit_and_upload_batch(
                    batch, token_manager,
                    model_version="vlm",
                    language=cfg.get("language", "ch"),
                    proxies=proxies,
                    proxy_mode=proxy_mode,
                    proxy_url=proxy_url,
                    on_uploaded=_on_uploaded,
                )
                # 只保留上传成功的文件
                success_nodes = [fn for fn in batch if upload_results.get(fn.path, False)]
                batch_node_map[batch_id] = success_nodes
                batch_token_idx[batch_id] = token_idx
                for fn in success_nodes:
                    self._statuses[fn] = FileStatus.PARSING
                    self._messages[fn] = "排队中"
            except Exception as exc:
                self._log_line(f"[错误] 批次 {i+1} 申请上传 URL 失败: {exc}")
                for fn in batch:
                    if self._statuses[fn] == FileStatus.UPLOADING:
                        self._statuses[fn] = FileStatus.FAILED
                        self._messages[fn] = f"失败: {exc}"
                        self._fail_count += 1

        if not batch_node_map:
            self._finished = True
            self._log_line("所有文件均上传失败，处理终止")
            return

        # ── 轮询阶段 ──
        self._log_line("开始轮询解析结果...")

        # 建立 file_name → FileNode 映射（同批）
        def on_batch_progress(bid: str, results: list[dict]) -> None:
            nodes = batch_node_map.get(bid, [])
            name_map = {fn.path.name: fn for fn in nodes}
            for r in results:
                fn = name_map.get(r.get("file_name", ""))
                if fn is None:
                    continue
                state = r.get("state", "")
                if state == "running":
                    prog = r.get("extract_progress", {})
                    total_p = prog.get("total_pages", "?")
                    done_p = prog.get("extracted_pages", "?")
                    self._statuses[fn] = FileStatus.PARSING
                    self._messages[fn] = f"解析中 ({done_p}/{total_p} 页)"
                elif state == "pending":
                    self._messages[fn] = "排队中"
                elif state == "done":
                    if self._statuses[fn] != FileStatus.DONE:
                        self._statuses[fn] = FileStatus.DOWNLOADING
                        self._messages[fn] = "准备下载"
                elif state == "failed":
                    if self._statuses[fn] != FileStatus.FAILED:
                        self._statuses[fn] = FileStatus.FAILED
                        self._messages[fn] = f"解析失败: {r.get('err_msg', '')}"
                        self._fail_count += 1
                        self._log_line(f"[失败] {fn.path.name}: {r.get('err_msg', '')}")

        batch_token_pairs = [(bid, batch_token_idx.get(bid, 0)) for bid in batch_node_map]
        poll_results = poll_batches_concurrent(
            batch_token_pairs,
            token_manager,
            interval=poll_interval,
            timeout=timeout,
            proxies=proxies,
            on_batch_progress=on_batch_progress,
        )

        # ── 下载阶段 ──
        download_tasks = []
        for bid, extract_results in poll_results.items():
            nodes = batch_node_map.get(bid, [])
            name_map = {fn.path.name: fn for fn in nodes}
            for r in extract_results:
                fn = name_map.get(r.get("file_name", ""))
                if fn is None or r.get("state") != "done":
                    continue
                zip_url = r.get("full_zip_url", "")
                if zip_url:
                    download_tasks.append((zip_url, fn, stem_map[fn]))

        if download_tasks:
            self._log_line(f"开始下载 {len(download_tasks)} 个结果...")

            def on_downloaded(fn: FileNode, md_path, err: str | None) -> None:
                if md_path:
                    self._statuses[fn] = FileStatus.DONE
                    self._messages[fn] = f"✓ → {fn.path.stem}.md"
                    self._done_count += 1
                    self._log_line(f"[完成] {fn.path.name} → {md_path.name}")
                else:
                    self._statuses[fn] = FileStatus.FAILED
                    self._messages[fn] = f"下载失败: {err}"
                    self._fail_count += 1
                    self._log_line(f"[失败] 下载 {fn.path.name}: {err}")

            download_results_concurrent(
                download_tasks, keep_zip, keep_json, proxies, on_downloaded
            )

        self._finished = True
        self._log_line(
            f"全部完成！成功 {self._done_count}，失败 {self._fail_count}，"
            f"跳过 {len(skipped)}"
        )

    def _log_line(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self._log.append(f"[{ts}] {msg}")
        logger.info(msg)

    def action_quit_app(self) -> None:
        self.app.exit()


# ══════════════════════════════════════════════════════════════════════════════
# MinerUApp
# ══════════════════════════════════════════════════════════════════════════════

class MinerUApp(App):
    """主应用，入口屏为 SelectScreen。"""

    CSS = CSS
    TITLE = "MinerU 批量解析下载工具"
    SUB_TITLE = "精准解析 · vlm 模型"

    def __init__(self, root_node: DirNode, cfg: dict, **kwargs):
        super().__init__(**kwargs)
        self._root_node = root_node
        self._cfg = cfg

    def on_mount(self) -> None:
        self.push_screen(SelectScreen(self._root_node, self._cfg))


def run_tui(root_node: DirNode, cfg: dict) -> None:
    """启动 TUI 应用的入口函数。"""
    app = MinerUApp(root_node=root_node, cfg=cfg)
    app.run()
