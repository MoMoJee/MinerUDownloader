"""解析完成即下载：调度器去重与 CLI 流水线行为测试。"""
from __future__ import annotations

import io
import threading
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import api
import cli
import processor
from processor import EagerDownloader
from scanner import DuplicateAction, FileNode, flatten_files, scan
from tui import FileStatus, ProgressScreen


def _make_node(root: Path, name: str) -> FileNode:
    return FileNode(path=root / name, rel_path=Path(name), size=1)


class EagerDownloaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_submit_deduplicates_and_calls_back(self) -> None:
        node = _make_node(self.root, "a.pdf")
        calls: list[tuple[str, str]] = []
        results: list[tuple] = []
        lock = threading.Lock()

        def fake_extract(zip_url, path, keep_zip, keep_json, stem, proxies=None):
            with lock:
                calls.append((zip_url, stem))
            return path.parent / f"{stem}.md"

        def on_done(fn, md_path, err):
            results.append((fn, md_path, err))

        with mock.patch("processor.extract_result", side_effect=fake_extract):
            downloader = EagerDownloader(False, False, None, on_done)
            threads = [
                threading.Thread(
                    target=downloader.submit,
                    args=("http://example/a.zip", node, "a"),
                )
                for _ in range(8)
            ]
            for t in threads:
                t.start()
            for _ in range(8):
                downloader.submit("http://example/a.zip", node, "a")
            for t in threads:
                t.join()
            downloader.wait()

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], node)
        self.assertEqual(results[0][2], None)

    def test_download_error_is_reported(self) -> None:
        node = _make_node(self.root, "b.pdf")
        results: list[tuple] = []

        def on_done(fn, md_path, err):
            results.append((fn, md_path, err))

        with mock.patch(
            "processor.extract_result", side_effect=RuntimeError("boom")
        ):
            downloader = EagerDownloader(False, False, None, on_done)
            downloader.submit("http://example/b.zip", node, "b")
            downloader.wait()

        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0][1])
        self.assertIn("boom", results[0][2])

    def test_empty_url_is_ignored(self) -> None:
        node = _make_node(self.root, "c.pdf")
        with mock.patch("processor.extract_result") as extract:
            downloader = EagerDownloader(False, False, None, lambda *a: None)
            self.assertFalse(downloader.submit("", node, "c"))
            downloader.wait()
        extract.assert_not_called()

    def test_real_zip_is_extracted(self) -> None:
        node = _make_node(self.root, "real.pdf")
        node.path.write_bytes(b"x")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("full.md", "# hi")
            zf.writestr("images/pic.png", b"png")
        results: list[tuple] = []

        with mock.patch(
            "processor._download_with_retry", return_value=buf.getvalue()
        ):
            downloader = EagerDownloader(
                False, False, None, lambda *a: results.append(a)
            )
            downloader.submit("http://example/real.zip", node, "real")
            downloader.wait()

        md_path = self.root / "real.md"
        self.assertEqual(md_path.read_text(encoding="utf-8"), "# hi")
        self.assertEqual((self.root / "images" / "pic.png").read_bytes(), b"png")
        self.assertEqual(results[0][1], md_path)


class CliEagerPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "a.pdf").write_bytes(b"a")
        (self.root / "b.pdf").write_bytes(b"b")
        self.nodes = flatten_files(scan(self.root))
        for fn in self.nodes:
            fn.duplicate_action = DuplicateAction.NONE
        self.a, self.b = sorted(self.nodes, key=lambda f: f.path.name)

    def _cfg(self) -> dict:
        return {
            "token": "test-token",
            "batch_size": 20,
            "poll_interval": 8,
            "timeout": 1800,
            "proxy_mode": "none",
            "proxy_url": "",
            "keep_zip": False,
            "keep_json": False,
            "language": "ch",
        }

    def _fake_submit(self, batch, token_manager, model_version="vlm",
                     language="ch", proxies=None, proxy_mode="system",
                     proxy_url="", on_uploaded=None):
        if on_uploaded:
            for fn in batch:
                on_uploaded(fn.path, True)
        return "batch-1", {fn.path: True for fn in batch}, 0

    def test_download_starts_during_poll_and_runs_once(self) -> None:
        download_started = threading.Event()
        download_finished = threading.Event()
        extracted: list[str] = []
        lock = threading.Lock()

        def fake_extract(zip_url, path, keep_zip, keep_json, stem, proxies=None):
            with lock:
                extracted.append(path.name)
            md = path.parent / f"{stem}.md"
            md.write_text("x", encoding="utf-8")
            if path.name == "a.pdf":
                download_started.set()
                download_finished.set()
            return md

        done_a = {
            "file_name": "a.pdf",
            "state": "done",
            "full_zip_url": "http://example/a.zip",
        }
        running_b = {"file_name": "b.pdf", "state": "running"}
        done_b = {
            "file_name": "b.pdf",
            "state": "done",
            "full_zip_url": "http://example/b.zip",
        }

        def fake_poll(pairs, token_manager, interval=8, timeout=1800,
                      proxies=None, on_batch_progress=None):
            on_batch_progress("batch-1", [done_a, running_b])
            on_batch_progress("batch-1", [done_a, running_b])
            if not download_started.wait(5):
                raise AssertionError("轮询结束前没有开始下载 a.pdf")
            if not download_finished.wait(5):
                raise AssertionError("a.pdf 下载未完成")
            # 下载完成后重复回调 done，验证不会把状态打回“准备下载”
            on_batch_progress("batch-1", [done_a, running_b])
            on_batch_progress("batch-1", [done_a, done_b])
            return {"batch-1": [done_a, done_b]}

        with mock.patch.object(api, "submit_and_upload_batch", side_effect=self._fake_submit), \
             mock.patch.object(api, "poll_batches_concurrent", side_effect=fake_poll), \
             mock.patch.object(processor, "extract_result", side_effect=fake_extract):
            result = cli._phase_process(
                self.nodes, self._cfg(), assume_yes=True, quiet=True, root=self.root
            )

        self.assertEqual(result["summary"]["done"], 2)
        self.assertEqual(result["summary"]["failed"], 0)
        self.assertEqual(sorted(extracted), ["a.pdf", "b.pdf"])
        self.assertTrue((self.root / "a.md").exists())
        self.assertTrue((self.root / "b.md").exists())
        statuses = {Path(f["path"]).name: f["status"] for f in result["files"]}
        self.assertEqual(statuses, {"a.pdf": "done", "b.pdf": "done"})

    def test_timeout_keeps_completed_download_as_success(self) -> None:
        download_finished = threading.Event()

        def fake_extract(zip_url, path, keep_zip, keep_json, stem, proxies=None):
            md = path.parent / f"{stem}.md"
            md.write_text("x", encoding="utf-8")
            if path.name == "a.pdf":
                download_finished.set()
            return md

        done_a = {
            "file_name": "a.pdf",
            "state": "done",
            "full_zip_url": "http://example/a.zip",
        }
        running_b = {"file_name": "b.pdf", "state": "running"}

        def fake_poll(pairs, token_manager, interval=8, timeout=1800,
                      proxies=None, on_batch_progress=None):
            on_batch_progress("batch-1", [done_a, running_b])
            if not download_finished.wait(5):
                raise AssertionError("a.pdf 下载未完成")
            return {}  # 模拟批次轮询超时：未返回任何结果

        with mock.patch.object(api, "submit_and_upload_batch", side_effect=self._fake_submit), \
             mock.patch.object(api, "poll_batches_concurrent", side_effect=fake_poll), \
             mock.patch.object(processor, "extract_result", side_effect=fake_extract):
            result = cli._phase_process(
                self.nodes, self._cfg(), assume_yes=True, quiet=True, root=self.root
            )

        statuses = {Path(f["path"]).name: f["status"] for f in result["files"]}
        self.assertEqual(statuses["a.pdf"], "done")
        self.assertEqual(statuses["b.pdf"], "failed")
        self.assertEqual(result["summary"]["done"], 1)
        self.assertEqual(result["summary"]["failed"], 1)
        self.assertTrue((self.root / "a.md").exists())
        self.assertFalse((self.root / "b.md").exists())


class TuiEagerPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "a.pdf").write_bytes(b"a")
        (self.root / "b.pdf").write_bytes(b"b")
        self.nodes = flatten_files(scan(self.root))
        for fn in self.nodes:
            fn.duplicate_action = DuplicateAction.NONE
        self.a, self.b = sorted(self.nodes, key=lambda f: f.path.name)

    def test_tui_downloads_while_polling(self) -> None:
        download_started = threading.Event()

        def fake_extract(zip_url, path, keep_zip, keep_json, stem, proxies=None):
            md = path.parent / f"{stem}.md"
            md.write_text("x", encoding="utf-8")
            if path.name == "a.pdf":
                download_started.set()
            return md

        def fake_submit(batch, token_manager, model_version="vlm",
                        language="ch", proxies=None, proxy_mode="system",
                        proxy_url="", on_uploaded=None):
            if on_uploaded:
                for fn in batch:
                    on_uploaded(fn.path, True)
            return "batch-1", {fn.path: True for fn in batch}, 0

        done_a = {
            "file_name": "a.pdf",
            "state": "done",
            "full_zip_url": "http://example/a.zip",
        }
        running_b = {"file_name": "b.pdf", "state": "running"}
        done_b = {
            "file_name": "b.pdf",
            "state": "done",
            "full_zip_url": "http://example/b.zip",
        }

        def fake_poll(pairs, token_manager, interval=8, timeout=1800,
                      proxies=None, on_batch_progress=None):
            on_batch_progress("batch-1", [done_a, running_b])
            if not download_started.wait(5):
                raise AssertionError("TUI 轮询结束前没有开始下载 a.pdf")
            on_batch_progress("batch-1", [done_a, running_b])
            on_batch_progress("batch-1", [done_a, done_b])
            return {"batch-1": [done_a, done_b]}

        screen = ProgressScreen(self.nodes, {"proxy_mode": "none"}, None)
        with mock.patch.object(api, "submit_and_upload_batch", side_effect=fake_submit), \
             mock.patch.object(api, "poll_batches_concurrent", side_effect=fake_poll), \
             mock.patch.object(processor, "extract_result", side_effect=fake_extract):
            screen._run_processing_inner()

        self.assertEqual(screen._statuses[self.a], FileStatus.DONE)
        self.assertEqual(screen._statuses[self.b], FileStatus.DONE)
        self.assertEqual(screen._done_count, 2)
        self.assertEqual(screen._fail_count, 0)
        self.assertIn("a.md", screen._messages[self.a])
        self.assertTrue((self.root / "a.md").exists())
        self.assertTrue((self.root / "b.md").exists())

    def test_tui_download_failure_not_reset_by_repeated_done(self) -> None:
        download_failed = threading.Event()

        def fake_extract(zip_url, path, keep_zip, keep_json, stem, proxies=None):
            if path.name == "a.pdf":
                download_failed.set()
                raise RuntimeError("boom")
            return path.parent / f"{stem}.md"

        def fake_submit(batch, token_manager, model_version="vlm",
                        language="ch", proxies=None, proxy_mode="system",
                        proxy_url="", on_uploaded=None):
            if on_uploaded:
                for fn in batch:
                    on_uploaded(fn.path, True)
            return "batch-1", {fn.path: True for fn in batch}, 0

        done_a = {
            "file_name": "a.pdf",
            "state": "done",
            "full_zip_url": "http://example/a.zip",
        }
        running_b = {"file_name": "b.pdf", "state": "running"}

        def fake_poll(pairs, token_manager, interval=8, timeout=1800,
                      proxies=None, on_batch_progress=None):
            on_batch_progress("batch-1", [done_a, running_b])
            if not download_failed.wait(5):
                raise AssertionError("a.pdf 下载失败回调未触发")
            # 下载失败后仍收到重复 done 回调，状态不能被重置
            on_batch_progress("batch-1", [done_a, running_b])
            return {"batch-1": [done_a, running_b]}

        screen = ProgressScreen(self.nodes, {"proxy_mode": "none"}, None)
        with mock.patch.object(api, "submit_and_upload_batch", side_effect=fake_submit), \
             mock.patch.object(api, "poll_batches_concurrent", side_effect=fake_poll), \
             mock.patch.object(processor, "extract_result", side_effect=fake_extract):
            screen._run_processing_inner()

        self.assertEqual(screen._statuses[self.a], FileStatus.FAILED)
        self.assertEqual(screen._fail_count, 1)


if __name__ == "__main__":
    unittest.main()
