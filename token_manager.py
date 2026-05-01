"""
token_manager.py — 多 Token 管理与负载均衡。

支持：
  - 轮询取下一个可用 Token（get_token）
  - 按错误分类标记 Token 状态（report_error）
  - TUI/CLI 手动切换 enabled（toggle_enabled）
  - 统计每个 Token 已分配批次数
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum

from errors import ErrorCategory, NoAvailableTokenError


class TokenState(Enum):
    ACTIVE       = "active"       # 正常可用
    RATE_LIMITED = "rate_limited" # 今日配额耗尽
    INVALID      = "invalid"      # Token 无效/过期


@dataclass
class TokenEntry:
    token: str
    state: TokenState = TokenState.ACTIVE
    enabled: bool = True          # 用户手动启用/禁用
    used_batches: int = 0         # 已分配批次数（统计）

    @property
    def available(self) -> bool:
        return self.state == TokenState.ACTIVE and self.enabled

    @property
    def display(self) -> str:
        """短显示字符串，含首末字符。"""
        t = self.token
        if len(t) > 12:
            short = f"{t[:6]}…{t[-4:]}"
        else:
            short = t
        state_icon = {
            TokenState.ACTIVE: "✓ ACTIVE",
            TokenState.RATE_LIMITED: "⏸ RATE_LIMITED",
            TokenState.INVALID: "✗ INVALID",
        }[self.state]
        en = "☑" if self.enabled else "☐"
        return f"{en} {short}  {state_icon}  {self.used_batches} 批"


class TokenManager:
    """
    管理一组 Token 的状态和负载均衡分配。
    线程安全（内部 Lock）。
    """

    def __init__(self, tokens: list[str], lb_enabled: bool = True) -> None:
        """
        tokens: 已去重的 Token 列表（至少 1 个）。
        lb_enabled: 是否启用负载均衡。False 时始终用 entries[0]。
        """
        if not tokens:
            raise ValueError("至少需要一个 Token")
        self.entries: list[TokenEntry] = [TokenEntry(t) for t in tokens]
        self.lb_enabled = lb_enabled
        self._lock = threading.Lock()
        self._rr_index = 0  # 轮询游标

    # ── 取 Token ──────────────────────────────────────────────────────────────

    def get_token(self) -> tuple[int, str]:
        """
        返回 (index, token_str)。
        - 负载均衡模式：轮询所有 available 的 Token。
        - 非负载均衡：始终返回第一个 available Token。
        若全部不可用，抛出 NoAvailableTokenError。
        """
        with self._lock:
            available = [i for i, e in enumerate(self.entries) if e.available]
            if not available:
                raise NoAvailableTokenError(
                    "所有 Token 均不可用（已耗尽配额或失效）。\n"
                    "请更换或重新启用 Token。"
                )
            if not self.lb_enabled or len(available) == 1:
                idx = available[0]
            else:
                # 从 available 列表中轮询
                self._rr_index = self._rr_index % len(available)
                idx = available[self._rr_index]
                self._rr_index += 1

            self.entries[idx].used_batches += 1
            return idx, self.entries[idx].token

    # ── 报告错误 ───────────────────────────────────────────────────────────────

    def report_error(self, idx: int, category: ErrorCategory) -> None:
        """
        根据错误分类更新 Token 状态。
        AUTH        → INVALID
        RATE_LIMIT  → RATE_LIMITED
        其他        → 不修改（由调用方决定重试逻辑）
        """
        with self._lock:
            if not (0 <= idx < len(self.entries)):
                return
            entry = self.entries[idx]
            if category == ErrorCategory.AUTH:
                entry.state = TokenState.INVALID
            elif category == ErrorCategory.RATE_LIMIT:
                entry.state = TokenState.RATE_LIMITED

    # ── 手动切换 ───────────────────────────────────────────────────────────────

    def toggle_enabled(self, idx: int) -> None:
        """翻转指定 Token 的 enabled 状态。"""
        with self._lock:
            if 0 <= idx < len(self.entries):
                self.entries[idx].enabled = not self.entries[idx].enabled

    def set_enabled(self, idx: int, value: bool) -> None:
        with self._lock:
            if 0 <= idx < len(self.entries):
                self.entries[idx].enabled = value

    # ── 查询 ──────────────────────────────────────────────────────────────────

    def has_available(self) -> bool:
        with self._lock:
            return any(e.available for e in self.entries)

    def count(self) -> int:
        return len(self.entries)

    def status_lines(self) -> list[str]:
        """返回每个 Token 的显示字符串列表，供 TUI/CLI 渲染。"""
        with self._lock:
            return [f"[{i}] {e.display}" for i, e in enumerate(self.entries)]

    # ── 工厂工具 ──────────────────────────────────────────────────────────────

    @staticmethod
    def parse_tokens(raw: str | list | None) -> list[str]:
        """
        将各种格式的 Token 输入解析为去重有序列表。
        - str: 按逗号分割，去空格
        - list: 直接用，各元素去空格
        - None / 空: 返回 []
        """
        if not raw:
            return []
        if isinstance(raw, list):
            tokens = [str(t).strip() for t in raw]
        else:
            tokens = [t.strip() for t in str(raw).split(",")]
        # 去空，去重保序
        seen: set[str] = set()
        result: list[str] = []
        for t in tokens:
            if t and t not in seen:
                seen.add(t)
                result.append(t)
        return result

    @classmethod
    def from_config(cls, cfg: dict, lb_enabled: bool | None = None) -> "TokenManager":
        """
        从配置字典构建 TokenManager。
        lb_enabled=None 时，Token 数量 > 1 则自动启用负载均衡。
        """
        tokens = cls.parse_tokens(cfg.get("token", ""))
        if not tokens:
            tokens = [""]  # 占位，后续会在 TUI/CLI 填入
        if lb_enabled is None:
            lb_enabled = len(tokens) > 1
        return cls(tokens, lb_enabled)
