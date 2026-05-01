"""
errors.py — MinerU API 错误码目录与统一错误类。

所有业务代码通过 parse_api_response() 统一抛出 MinerUApiError，
不在各处硬编码错误码字符串。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ErrorCategory(Enum):
    """错误大类，决定上层的重试/切换/放弃策略。"""
    AUTH        = "auth"        # Token 无效/过期 → 标记 Token 失效，切换
    RATE_LIMIT  = "rate_limit"  # 日配额耗尽 → 标记 Token 今日耗尽，切换
    TRANSIENT   = "transient"   # 暂时性故障 → 按重试逻辑处理
    FILE_ERROR  = "file_error"  # 文件本身问题 → 标记文件失败，不重试
    CONFIG      = "config"      # 调用参数/格式错误 → 报告给用户
    UNKNOWN     = "unknown"     # 兜底


@dataclass(frozen=True)
class ErrorInfo:
    message: str            # 简短错误说明
    hint: str               # 建议操作
    category: ErrorCategory


# ── 错误码目录 ────────────────────────────────────────────────────────────────
# key: API 返回的 code 字段（转为字符串）
ERROR_CATALOG: dict[str, ErrorInfo] = {
    # Token 问题
    "A0202": ErrorInfo(
        "Token 错误",
        "检查 Token 是否正确，确保有 Bearer 前缀，或更换新 Token",
        ErrorCategory.AUTH,
    ),
    "A0211": ErrorInfo(
        "Token 过期",
        "更换新 Token",
        ErrorCategory.AUTH,
    ),
    # 参数/调用错误
    "-500": ErrorInfo(
        "传参错误",
        "确保参数类型及 Content-Type 正确",
        ErrorCategory.CONFIG,
    ),
    "-10002": ErrorInfo(
        "请求参数错误",
        "检查请求参数格式",
        ErrorCategory.CONFIG,
    ),
    "-60008": ErrorInfo(
        "文件读取超时",
        "检查上传 URL 是否可访问",
        ErrorCategory.CONFIG,
    ),
    "-60011": ErrorInfo(
        "获取有效文件失败",
        "确保文件已成功上传后再提交",
        ErrorCategory.CONFIG,
    ),
    "-60012": ErrorInfo(
        "找不到任务",
        "确保 batch_id 有效且未被删除",
        ErrorCategory.CONFIG,
    ),
    "-60013": ErrorInfo(
        "没有权限访问该任务",
        "只能访问自己提交的任务，检查 Token 是否匹配",
        ErrorCategory.CONFIG,
    ),
    # 暂时性故障
    "-10001": ErrorInfo(
        "服务异常",
        "请稍后再试",
        ErrorCategory.TRANSIENT,
    ),
    "-60001": ErrorInfo(
        "生成上传 URL 失败",
        "请稍后再试",
        ErrorCategory.TRANSIENT,
    ),
    "-60007": ErrorInfo(
        "模型服务暂时不可用",
        "请稍后重试或联系技术支持",
        ErrorCategory.TRANSIENT,
    ),
    "-60009": ErrorInfo(
        "任务提交队列已满",
        "请稍后再试",
        ErrorCategory.TRANSIENT,
    ),
    "-60010": ErrorInfo(
        "解析失败",
        "请稍后再试",
        ErrorCategory.TRANSIENT,
    ),
    "-60020": ErrorInfo(
        "文件拆分失败",
        "请稍后重试",
        ErrorCategory.TRANSIENT,
    ),
    "-60021": ErrorInfo(
        "读取文件页数失败",
        "请稍后重试",
        ErrorCategory.TRANSIENT,
    ),
    "-60022": ErrorInfo(
        "网页读取失败",
        "可能因网络问题或限频，请稍后重试",
        ErrorCategory.TRANSIENT,
    ),
    # 每日配额耗尽
    "-60018": ErrorInfo(
        "每日解析任务数量已达上限",
        "明日再来，或切换其他 Token",
        ErrorCategory.RATE_LIMIT,
    ),
    "-60019": ErrorInfo(
        "html 文件解析额度不足",
        "明日再来，或切换其他 Token",
        ErrorCategory.RATE_LIMIT,
    ),
    # 文件本身问题
    "-60002": ErrorInfo(
        "获取匹配的文件格式失败",
        "检查文件名后缀，确保为 pdf/doc/docx/ppt/pptx/png/jpg/jpeg 之一",
        ErrorCategory.FILE_ERROR,
    ),
    "-60003": ErrorInfo(
        "文件读取失败",
        "检查文件是否损坏并重新上传",
        ErrorCategory.FILE_ERROR,
    ),
    "-60004": ErrorInfo(
        "空文件",
        "请上传有效的非空文件",
        ErrorCategory.FILE_ERROR,
    ),
    "-60005": ErrorInfo(
        "文件大小超出限制",
        "最大支持 200MB，请拆分后重试",
        ErrorCategory.FILE_ERROR,
    ),
    "-60006": ErrorInfo(
        "文件页数超过限制",
        "请拆分文件后重试",
        ErrorCategory.FILE_ERROR,
    ),
    "-60014": ErrorInfo(
        "删除运行中的任务",
        "运行中的任务暂不支持删除",
        ErrorCategory.FILE_ERROR,
    ),
    "-60015": ErrorInfo(
        "文件转换失败",
        "可以手动转为 PDF 再上传",
        ErrorCategory.FILE_ERROR,
    ),
    "-60016": ErrorInfo(
        "文件转换为指定格式失败",
        "尝试其他格式导出或重试",
        ErrorCategory.FILE_ERROR,
    ),
    "-60017": ErrorInfo(
        "重试次数达到上限",
        "等待模型升级后重试",
        ErrorCategory.FILE_ERROR,
    ),
}

# 默认兜底
_UNKNOWN_INFO = ErrorInfo("未知错误", "请检查 API 响应或联系技术支持", ErrorCategory.UNKNOWN)


def get_error_info(code: int | str) -> ErrorInfo:
    """根据错误码获取 ErrorInfo，未收录则返回 UNKNOWN。"""
    return ERROR_CATALOG.get(str(code), _UNKNOWN_INFO)


class MinerUApiError(Exception):
    """MinerU API 返回非 0 code 时抛出的统一异常。"""

    def __init__(self, code: int | str, msg: str = "") -> None:
        self.code = str(code)
        self.api_msg = msg
        self.info = get_error_info(code)
        display = f"[{self.code}] {self.info.message}"
        if msg and msg != self.info.message:
            display += f" — {msg}"
        display += f"  提示：{self.info.hint}"
        super().__init__(display)


class NoAvailableTokenError(Exception):
    """所有 Token 均不可用时抛出。"""
    pass


def parse_api_response(data: dict) -> None:
    """
    检查 API 响应体，若 code != 0 则抛出 MinerUApiError。
    在 api.py 每次 HTTP 响应后调用。
    """
    code = data.get("code", 0)
    if code == 0:
        return
    msg = data.get("msg", "")
    raise MinerUApiError(code, msg)
