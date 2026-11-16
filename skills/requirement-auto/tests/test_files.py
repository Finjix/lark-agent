"""Unpacking zips, the retention sweep, and small fixes (unzip.test.ts, retention.test.ts, fixes.test.ts)."""

from __future__ import annotations

import os
import time
import unittest

from tests.helpers import make_zip, temp_dir, write_file

from gof.cls_adapter import draft_id_of
from gof.cls_guard import check_pin, pin_state
from gof.cls_session import account_names, check_token
from gof.creative import material_refusal
from gof.paths import resolve_data_paths
from gof.retention import prune_expired, retention_days
from gof.unzip import extract_entry, list_zip, unpack_zip
from gof.util import Blocked, write_json_new
from gof.web import FetchError, Response


def zip_file(name: str, data: bytes) -> str:
    return write_file(os.path.join(temp_dir("zip-"), name), data)


def listing(directory: str) -> list[str]:
    return sorted(os.listdir(directory))


class UnzipTests(unittest.TestCase):
    def test_zipped_folder(self) -> None:
        """A zipped folder unpacks to its files, flat; deeper folders, other formats and system junk are left out with the reason."""
        video = bytes([7]) * 300_000
        file = zip_file("素材.zip", make_zip([
            {"name": "新建文件夹/GOFCN66367_片头.mp4", "data": video},
            {"name": "新建文件夹/封面.png", "data": "png", "store": True},
            {"name": "新建文件夹/说明.txt", "data": "txt"},
            {"name": "新建文件夹/子文件夹/c.mp4", "data": "c"},
            {"name": "新建文件夹/.DS_Store", "data": "x"},
            {"name": "__MACOSX/新建文件夹/._封面.png", "data": "x"},
        ]))
        root = temp_dir("unpacked-")
        result = unpack_zip(file, root, material_refusal)
        self.assertEqual(listing(result.dir), ["GOFCN66367_片头.mp4", "封面.png"])
        with open(os.path.join(result.dir, "GOFCN66367_片头.mp4"), "rb") as handle:
            self.assertEqual(handle.read(), video, "deflated data comes back exactly")
        self.assertEqual([[s["file"], s["reason"]] for s in result.skipped], [
            ["素材.zip：新建文件夹/说明.txt", "CLS 不支持这种格式（.txt）"],
            ["素材.zip：新建文件夹/子文件夹/c.mp4", "压缩包里的子文件夹不读取（文件夹不可嵌套）"]])
        # The same zip again: the folder already unpacked is used as it is.
        again = unpack_zip(file, root, material_refusal)
        self.assertEqual(again.dir, result.dir)
        self.assertEqual(again.skipped, result.skipped)

    def test_gbk_and_zip64(self) -> None:
        """Names written by Chinese Windows tools (GBK, no UTF-8 flag) and zip64 records are read."""
        gbk = bytes.fromhex("b2e2cad4cbd8b2c42e6d7034")  # 测试素材.mp4
        entries = list_zip(zip_file("gbk.zip", make_zip([{"name": gbk, "data": "v"}, {"name": "plain.png", "data": "p"}])))
        self.assertEqual([e.name for e in entries], ["测试素材.mp4", "plain.png"])
        big = zip_file("z64.zip", make_zip([{"name": "a.mp4", "data": bytes([1]) * 5000}, {"name": "图.jpg", "data": "j"}], zip64=True))
        result = unpack_zip(big, temp_dir("unpacked-"), material_refusal)
        self.assertEqual(listing(result.dir), ["a.mp4", "图.jpg"])
        self.assertEqual(os.path.getsize(os.path.join(result.dir, "a.mp4")), 5000)

    def test_damaged_and_unsafe(self) -> None:
        """A damaged entry is refused and nothing of it is left; unsafe paths are never written."""
        bad = zip_file("bad.zip", make_zip([{"name": "a.mp4", "data": "abc", "bad_crc": True}]))
        root = temp_dir("unpacked-")
        self.assertRaisesRegex(Blocked, "已损坏", unpack_zip, bad, root, material_refusal)
        folder = os.listdir(root)[0]
        self.assertEqual(os.listdir(os.path.join(root, folder, "files")), [])
        evil = zip_file("evil.zip", make_zip([{"name": "../evil.mp4", "data": "x"}, {"name": "ok.mp4", "data": "y"}]))
        out = temp_dir("unpacked-")
        result = unpack_zip(evil, out, material_refusal)
        self.assertEqual(os.listdir(result.dir), ["ok.mp4"])
        self.assertEqual(result.skipped[0]["reason"], "路径不安全，没有解压")
        self.assertFalse(os.path.exists(os.path.join(out, "evil.mp4")))
        self.assertRaisesRegex(Blocked, "不是有效的 zip", list_zip, zip_file("not.zip", b"not a zip"))

    def test_windows_names(self) -> None:
        """Names a Windows file cannot hold are stored with _ and said so; two that come out the same are one file, not a failed zip."""
        file = zip_file("mac.zip", make_zip([{"name": "素材/为什么?_竖版.mp4", "data": "one"}, {"name": "素材/为什么*_竖版.mp4", "data": "two"},
                                              {"name": "素材/对抗:竖版.mp4", "data": "three"}, {"name": "素材/ok.mp4", "data": "four"}]))
        root = temp_dir("unpacked-")
        result = unpack_zip(file, root, material_refusal)
        self.assertEqual(listing(result.dir), sorted(["ok.mp4", "为什么__竖版.mp4", "对抗_竖版.mp4"]))
        with open(os.path.join(result.dir, "为什么__竖版.mp4"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "one", "the first of the two is kept")
        self.assertEqual(result.renamed, [{"file": "mac.zip：素材/为什么?_竖版.mp4", "as": "为什么__竖版.mp4"}, {"file": "mac.zip：素材/对抗:竖版.mp4", "as": "对抗_竖版.mp4"}])
        self.assertEqual([s["file"] for s in result.skipped], ["mac.zip：素材/为什么*_竖版.mp4"])
        self.assertRegex(result.skipped[0]["reason"], r"和“为什么\?_竖版\.mp4”换掉 Windows 文件名不能有的字符后同名")
        self.assertEqual(unpack_zip(file, root, material_refusal).renamed, result.renamed, "said again when the unpacked folder is used again")

    def test_existing_target(self) -> None:
        """An entry that cannot be written leaves a file of that name that was there already."""
        file = zip_file("one.zip", make_zip([{"name": "a.mp4", "data": "new"}]))
        entry = list_zip(file)[0]
        target = write_file(os.path.join(temp_dir("target-"), "a.mp4"), "already here")
        self.assertRaisesRegex(Blocked, "解压失败", extract_entry, file, entry, target)
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "already here")


DAY = 86_400_000


def later(days: float) -> float:
    """Files cannot be backdated, so time moves instead: what is 'now' for the sweep is `days` after the files were made."""
    return time.time() * 1000 + days * DAY


class RetentionTests(unittest.TestCase):
    def test_days(self) -> None:
        """The retention period is 7 days unless the environment gives a whole number of days."""
        self.assertEqual(retention_days({}), 7)
        self.assertEqual(retention_days({"REQUIREMENT_AUTO_RETENTION_DAYS": "3"}), 3)
        for bad in ("0", "-2", "1.5", "abc", ""):
            self.assertEqual(retention_days({"REQUIREMENT_AUTO_RETENTION_DAYS": bad}), 7)

    def test_old_go_logins_stay(self) -> None:
        """Old batches and temporary files go; the logins next to them, and anything still fresh, stay."""
        paths = resolve_data_paths({"LARK_AGENT_USER_DIR": temp_dir("ra-ret-")})
        batch = write_file(os.path.join(paths.batches, "abc123", "manifest.json"), "x")
        write_file(os.path.join(paths.batches, "abc123", "nested", "evidence.json"), "x")
        snapshot = write_file(os.path.join(paths.tmp, "snap.xlsx"), "x")
        qr = write_file(os.path.join(paths.cls, "login-qr.json"), "x")
        account = write_file(os.path.join(paths.root, "cls", "account.json"), "x")
        profile = write_file(os.path.join(paths.root, "cls", "browser-profile", "Default", "Cookies"), "x")
        self.assertEqual(prune_expired(paths, later(6)), 0, "inside the period nothing goes")
        self.assertTrue(os.path.exists(batch) and os.path.exists(snapshot))
        self.assertEqual(prune_expired(paths, later(8)), 2, "the batch folder and the temporary file")
        self.assertTrue(not os.path.exists(os.path.join(paths.batches, "abc123")) and not os.path.exists(snapshot))
        self.assertTrue(os.path.exists(paths.batches) and os.path.exists(paths.tmp), "the folders themselves stay")
        for kept in (qr, account, profile):
            self.assertTrue(os.path.exists(kept), f"{kept} is login data and stays")

    def test_batch_whole(self) -> None:
        """A batch is removed whole or not at all: one recently touched file keeps all of it."""
        paths = resolve_data_paths({"LARK_AGENT_USER_DIR": temp_dir("ra-ret-")})
        old = write_file(os.path.join(paths.batches, "busy", "manifest.json"), "x")
        recent = write_file(os.path.join(paths.batches, "busy", "events.jsonl"), "x")
        now = time.time() * 1000 + 8 * DAY
        # A file written 'now' (8 days on) is the one still in use.
        os.utime(recent, (now / 1000, now / 1000))
        self.assertEqual(prune_expired(paths, now), 0)
        self.assertTrue(os.path.exists(old) and os.path.exists(recent))

    def test_unpacked_each(self) -> None:
        """Every unpacked zip is judged on its own: one unpacked recently does not keep the older ones."""
        paths = resolve_data_paths({"LARK_AGENT_USER_DIR": temp_dir("ra-ret-")})
        old_zip = write_file(os.path.join(paths.tmp, "unpacked", "aaaa-old", "files", "big.mp4"), "x")
        write_file(os.path.join(paths.tmp, "unpacked", "aaaa-old", "complete.json"), "x")
        recent = write_file(os.path.join(paths.tmp, "unpacked", "bbbb-new", "complete.json"), "x")
        now = later(8)
        os.utime(recent, (now / 1000, now / 1000))
        self.assertEqual(prune_expired(paths, now), 1)
        self.assertFalse(os.path.exists(os.path.dirname(os.path.dirname(old_zip))), "the old one goes")
        self.assertTrue(os.path.exists(recent), "the recent one stays")
        self.assertTrue(os.path.exists(os.path.join(paths.tmp, "unpacked")), "and so does the folder that holds them")

    def test_links(self) -> None:
        """A missing folder is fine, and a link in place of a folder is not followed."""
        paths = resolve_data_paths({"LARK_AGENT_USER_DIR": temp_dir("ra-ret-")})
        self.assertEqual(prune_expired(paths, later(30)), 0)
        outside = write_file(os.path.join(temp_dir("ra-out-"), "keep.txt"), "x")
        os.makedirs(paths.root, exist_ok=True)
        try:
            import _winapi  # type: ignore[import-not-found]

            _winapi.CreateJunction(os.path.dirname(outside), paths.tmp)
        except Exception:
            try:
                os.symlink(os.path.dirname(outside), paths.tmp, target_is_directory=True)
            except OSError:
                return  # no right to make links here
        self.assertEqual(prune_expired(paths, later(30)), 0)
        self.assertTrue(os.path.exists(outside))


def answer(status: int, body: object):
    import json

    def fetcher(*args, **kwargs) -> Response:
        return Response(status, {"Content-Type": "application/json"}, json.dumps(body).encode())

    return fetcher


class FixesTests(unittest.TestCase):
    def test_corrupt_pin(self) -> None:
        """An unreadable account pin is a refusal, never "no pin yet"."""
        directory = temp_dir("ra-pin-")
        file = os.path.join(directory, "account.json")
        self.assertEqual(pin_state(file), {"state": "none"})
        for text in ("", '{"id":', '{"id":"7"}', "null"):
            write_file(file, text)
            self.assertEqual(pin_state(file)["state"], "corrupt", text)
            self.assertRaisesRegex(Blocked, "损坏", check_pin, file, {"id": 7, "name": "张三"})
            with open(file, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), text, "the file is left for an administrator")
        write_file(file, '{"id":7,"name":"张三","pinned_at":"x"}')
        self.assertEqual(pin_state(file)["state"], "ok")
        check_pin(file, {"id": 7, "name": "张三"})

    def test_check_token(self) -> None:
        """The CLS server saying no is told apart from not being able to ask."""
        good = {"code": 0, "data": {"id": 7, "name": "张三", "status": 1}}
        self.assertEqual(check_token("http://x", "Bearer a", answer(200, good)), {"account": {"id": 7, "name": "张三"}})
        self.assertEqual(check_token("http://x", "Bearer a", answer(401, {"code": 401})), {"rejected": True})
        self.assertEqual(check_token("http://x", "Bearer a", answer(200, {"code": 0, "data": {"id": 7, "status": 0}})), {"rejected": True})
        self.assertEqual(check_token("http://x", "Bearer a", answer(200, None)), {"rejected": True})
        for code in (502, 503, 429):
            self.assertEqual(check_token("http://x", "Bearer a", answer(code, {})), {"unreachable": True}, str(code))

        def refused(*args, **kwargs):
            raise FetchError("ECONNREFUSED")

        self.assertEqual(check_token("http://x", "Bearer a", refused), {"unreachable": True})

    def test_account_names(self) -> None:
        """The consent page may show the company name or one part of "name（alias）"."""
        self.assertEqual(account_names("张三"), ["张三"])
        self.assertEqual(sorted(account_names("张三（Zhang San）")), sorted(["Zhang San", "张三", "张三（Zhang San）"]))
        self.assertEqual(account_names("A"), [], "one character would match anything")

    def test_draft_id(self) -> None:
        """A save response gives its draft ID as a number or digits, at the top or under script; anything else is not an ID."""
        self.assertEqual(draft_id_of({"id": 12}), 12)
        self.assertEqual(draft_id_of({"id": "12"}), 12)
        self.assertEqual(draft_id_of({"script": {"id": 5}}), 5)
        for bad in (None, {}, {"id": 0}, {"id": -1}, {"id": "1e3"}, {"id": 1.5}, {"id": "x"}):
            self.assertIsNone(draft_id_of(bad), repr(bad))

    def test_exclusive(self) -> None:
        """Files are created exclusively and never half-made."""
        directory = temp_dir("ra-new-")
        file = os.path.join(directory, "a.json")
        write_json_new(file, {"a": 1})
        self.assertRaisesRegex(FileExistsError, "EEXIST", write_json_new, file, {"a": 2})
        with open(file, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), '{\n  "a": 1\n}\n')
        self.assertEqual(os.listdir(directory), ["a.json"], "no side file is left, after success or after a refusal")


if __name__ == "__main__":
    unittest.main()
