"""
jobs.py — 一次性 CLI 的后台作业管理。

后台作业不使用守护进程：启动时创建一个 job 目录，保存 job.json、stdout.json
和 stderr.log；状态通过 PID + 结果文件推断。
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TERMINAL = {"done", "failed"}
_TAIL_LIMIT = 4000


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def job_root() -> Path:
    override = os.environ.get("MINERU_JOB_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return _base_dir() / ".mineru_jobs"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def validate_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not _JOB_ID_RE.match(job_id):
        raise ValueError(f"非法 job id: {job_id!r}")
    return job_id


def _job_dir(job_id: str) -> Path:
    return job_root() / validate_job_id(job_id)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _tail(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-_TAIL_LIMIT:]


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [
            ctypes.c_ulong,
            ctypes.c_bool,
            ctypes.c_ulong,
        ]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _mask_args(argv: list[str]) -> list[str]:
    masked: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--token":
            masked.append(arg)
            masked.append("***")
            i += 2
            continue
        if arg.startswith("--token="):
            masked.append("--token=***")
        else:
            masked.append(arg)
        i += 1
    return masked


def _child_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve().parent / "main.py")]


def public_status(meta: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "job_id",
        "status",
        "pid",
        "created_at",
        "finished_at",
        "exit_code",
        "root_dir",
        "job_dir",
        "stdout",
        "stderr",
        "summary",
        "error",
    )
    result = {key: meta.get(key) for key in keys}
    if meta.get("stderr_tail"):
        result["stderr_tail"] = meta["stderr_tail"]
    return result


def emit(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=True, indent=2))


def _finalize(meta: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
    updated = dict(meta)
    updated["finished_at"] = _now()

    if isinstance(result, dict) and isinstance(result.get("success"), bool):
        success = result["success"]
        updated["status"] = "done" if success else "failed"
        updated["summary"] = result.get("summary")
        updated["error"] = result.get("error")
        if success:
            updated["exit_code"] = 0
        elif result.get("summary") is not None:
            updated["exit_code"] = 1
        else:
            updated["exit_code"] = 2
    else:
        updated["status"] = "failed"
        updated["summary"] = None
        updated["error"] = "job ended without a valid JSON result"
        updated["exit_code"] = 2
        updated["stderr_tail"] = _tail(Path(str(meta.get("stderr", ""))))

    return updated


def start_background(
    child_args: list[str],
    root_dir: Path,
) -> dict[str, Any]:
    base = job_root()
    base.mkdir(parents=True, exist_ok=True)

    job_id = ""
    job_dir = Path()
    for _ in range(5):
        candidate = (
            time.strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        candidate_dir = base / candidate
        try:
            candidate_dir.mkdir()
        except FileExistsError:
            continue
        job_id = candidate
        job_dir = candidate_dir
        break
    if not job_id:
        raise RuntimeError("无法创建 job 目录")

    if os.name != "nt":
        try:
            job_dir.chmod(0o700)
        except OSError:
            pass

    stdout_path = job_dir / "stdout.json"
    stderr_path = job_dir / "stderr.log"
    meta_path = job_dir / "job.json"

    args = list(child_args)
    if "--json" not in args:
        args.insert(0, "--json")
    cmd = _child_command() + args

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        flags = 0
        for name in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
            flags |= getattr(subprocess, name, 0)
        popen_kwargs["creationflags"] = flags
    else:
        popen_kwargs["start_new_session"] = True

    try:
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            proc = subprocess.Popen(
                cmd,
                cwd=os.getcwd(),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                env=env,
                close_fds=True,
                **popen_kwargs,
            )
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise

    meta: dict[str, Any] = {
        "job_id": job_id,
        "status": "running",
        "pid": proc.pid,
        "created_at": _now(),
        "finished_at": None,
        "exit_code": None,
        "cwd": os.getcwd(),
        "root_dir": str(root_dir),
        "command": _mask_args(args),
        "job_dir": str(job_dir),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "summary": None,
        "error": None,
    }
    try:
        _write_json(meta_path, meta)
    except Exception:
        try:
            proc.terminate()
        except OSError:
            pass
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    return meta


def get_status(job_id: str) -> dict[str, Any]:
    job_id = validate_job_id(job_id)
    job_dir = _job_dir(job_id)
    meta_path = job_dir / "job.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"未找到 job: {job_id}")
    meta = _read_json(meta_path)
    if meta is None:
        raise ValueError(f"job.json 损坏: {job_id}")

    if meta.get("status") in _TERMINAL:
        return public_status(meta)

    try:
        pid = int(meta.get("pid", -1))
    except (TypeError, ValueError):
        pid = -1
    if _pid_alive(pid):
        return public_status(meta)

    result = _read_json(Path(str(meta.get("stdout", ""))))
    updated = _finalize(meta, result)
    _write_json(meta_path, updated)
    return public_status(updated)


def wait_job(job_id: str, timeout: int = 3600, interval: float = 1.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout if timeout > 0 else None
    while True:
        meta = get_status(job_id)
        if meta.get("status") in _TERMINAL:
            return meta
        if deadline is not None and time.monotonic() >= deadline:
            meta = dict(meta)
            meta["error"] = f"等待超时（{timeout} 秒）"
            return meta
        time.sleep(interval)


def get_result(job_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    meta = get_status(job_id)
    if meta.get("status") == "running":
        raise RuntimeError(f"job 仍在运行: {job_id}")

    result = _read_json(Path(str(meta.get("stdout", ""))))
    if result is None:
        result = {
            "success": False,
            "job_id": job_id,
            "status": meta.get("status"),
            "error": meta.get("error") or "job ended without a valid JSON result",
            "stderr_tail": _tail(Path(str(meta.get("stderr", "")))),
        }
    return result, meta
