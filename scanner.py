"""
scanner.py — 递归扫描目录，构建 FileNode / DirNode 树，检测重复 .md。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from config import SUPPORTED_EXTENSIONS


class DuplicateAction(Enum):
    NONE = "none"           # 尚未决定（已存在 .md 时的初始状态）
    OVERWRITE = "overwrite" # 删除旧 .md，重新解析写入
    SKIP = "skip"           # 跳过，不提交解析
    RENAME = "rename"       # 输出为 {stem}_1.md，{stem}_2.md …


@dataclass(unsafe_hash=True)
class FileNode:
    path: Path                              # 绝对路径
    rel_path: Path                          # 相对于扫描根目录的路径
    size: int                               # 文件大小（字节）
    selected: bool = True
    existing_md: Path | None = None         # 若已有同名 .md，记录其路径
    duplicate_action: DuplicateAction = DuplicateAction.NONE

    @property
    def has_duplicate(self) -> bool:
        return self.existing_md is not None

    @property
    def size_str(self) -> str:
        """人类可读的文件大小字符串。"""
        b = self.size
        for unit in ("B", "KB", "MB", "GB"):
            if b < 1024:
                return f"{b:.1f} {unit}" if unit != "B" else f"{b} B"
            b /= 1024
        return f"{b:.1f} TB"


@dataclass
class DirNode:
    path: Path
    rel_path: Path
    children: list[FileNode | DirNode] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        """递归统计子树中的文件节点数量。"""
        count = 0
        for child in self.children:
            if isinstance(child, FileNode):
                count += 1
            else:
                count += child.file_count
        return count

    @property
    def selected_count(self) -> int:
        count = 0
        for child in self.children:
            if isinstance(child, FileNode):
                count += 1 if child.selected else 0
            else:
                count += child.selected_count
        return count

    @property
    def total_size(self) -> int:
        """递归统计所有文件大小。"""
        total = 0
        for child in self.children:
            if isinstance(child, FileNode):
                total += child.size
            else:
                total += child.total_size
        return total

    @property
    def selected_size(self) -> int:
        total = 0
        for child in self.children:
            if isinstance(child, FileNode):
                total += child.size if child.selected else 0
            else:
                total += child.selected_size
        return total


def scan(root: Path, on_progress: "Callable[[str, int], None] | None" = None) -> DirNode:
    """
    递归扫描 root 目录，返回根 DirNode。
    - 跳过以 '.' 开头的隐藏目录
    - 只保留 SUPPORTED_EXTENSIONS 中的文件（大小写不敏感）
    - 扫描后检测每个 FileNode 对应的 .md 是否已存在
    - 空目录节点会被裁剪（不出现在树中）
    on_progress(file_name, total_count)：每发现一个文件时回调（可选）
    """
    root = root.resolve()
    counter = [0]

    def _progress_cb(name: str) -> None:
        counter[0] += 1
        if on_progress:
            on_progress(name, counter[0])

    node = _scan_dir(root, root, _progress_cb)
    if node is None:
        return DirNode(path=root, rel_path=Path("."), children=[])
    return node


def _safe_is_file(p: Path) -> bool:
    try:
        return p.is_file()
    except OSError:
        return False


def _safe_is_dir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def _scan_dir(path: Path, root: Path, progress_cb: "Callable[[str], None] | None" = None) -> DirNode | None:
    """递归构建 DirNode；若目录内没有支持的文件则返回 None。"""
    rel = path.relative_to(root) if path != root else Path(".")
    node = DirNode(path=path, rel_path=rel)

    try:
        entries = sorted(path.iterdir(), key=lambda p: (_safe_is_file(p), p.name.lower()))
    except PermissionError:
        return None

    for entry in entries:
        if _safe_is_dir(entry):
            if entry.name.startswith("."):
                continue
            if entry.name.lower() == "images":  # 跳过 images 文件夹（大小写不敏感）
                continue
            child = _scan_dir(entry, root, progress_cb)
            if child is not None:
                node.children.append(child)
        elif _safe_is_file(entry):
            if entry.suffix.lower() in SUPPORTED_EXTENSIONS:
                file_node = _make_file_node(entry, root)
                if file_node is not None:
                    node.children.append(file_node)
                    if progress_cb:
                        progress_cb(entry.name)

    if not node.children:
        return None
    return node


def _make_file_node(path: Path, root: Path) -> FileNode | None:
    """创建 FileNode，检测是否已有同名 .md。失败（无法 stat）返回 None。"""
    rel = path.relative_to(root)
    try:
        size = path.stat().st_size
    except OSError:
        return None
    md_candidate = path.parent / (path.stem + ".md")
    existing_md = md_candidate if md_candidate.exists() else None

    return FileNode(
        path=path,
        rel_path=rel,
        size=size,
        selected=existing_md is None,   # 已有同名 .md 时默认不选中
        existing_md=existing_md,
        duplicate_action=DuplicateAction.NONE,
    )


def flatten_files(node: DirNode) -> list[FileNode]:
    """按深度优先顺序返回树中所有 FileNode 的扁平列表。"""
    result: list[FileNode] = []
    _collect(node, result)
    return result


def _collect(node: DirNode, result: list[FileNode]) -> None:
    for child in node.children:
        if isinstance(child, FileNode):
            result.append(child)
        else:
            _collect(child, result)


def selected_files(node: DirNode) -> list[FileNode]:
    """返回所有 selected=True 且 duplicate_action != SKIP 的 FileNode。"""
    return [
        f for f in flatten_files(node)
        if f.selected and f.duplicate_action != DuplicateAction.SKIP
    ]


def resolve_output_stem(file_node: FileNode) -> str:
    """
    根据 duplicate_action 确定输出文件名主干：
    - OVERWRITE / NONE（无 .md）→ file_node.path.stem
    - RENAME → 自动递增找到可用的 {stem}_N
    """
    stem = file_node.path.stem
    if file_node.duplicate_action != DuplicateAction.RENAME:
        return stem

    parent = file_node.path.parent
    n = 1
    while (parent / f"{stem}_{n}.md").exists():
        n += 1
    return f"{stem}_{n}"
