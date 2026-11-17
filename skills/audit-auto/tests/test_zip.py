import os
import unittest

from audit_sdk.errors import Blocked
from audit_sdk.media import refusal
from audit_sdk.unzip import unpack_zip

from tests.helpers.tmp import temp_dir
from tests.helpers.zip_writer import ZipFile, make_zip


def zip_file(name: str, data: bytes) -> str:
    at = os.path.join(temp_dir("zip-"), name)
    with open(at, "wb") as handle:
        handle.write(data)
    return at


class ZipTest(unittest.TestCase):
    def test_a_material_zip_unpacks_its_videos_and_images_at_any_depth_named_by_their_path(self) -> None:
        video = bytes([7]) * 300_000
        archive = zip_file("素材.zip", make_zip([
            ZipFile("横版/GOFCN66367_片头.mp4", video),
            ZipFile("竖版/GOFCN66367_片头.mp4", video, store=True),
            ZipFile(bytes([0xB7, 0xE2, 0xC3, 0xE6, 0x2E, 0x70, 0x6E, 0x67]), "png"),  # 封面.png in GBK, no UTF-8 flag
            ZipFile("说明.txt", "txt"),
            ZipFile("../逃出去.mp4", "x"),
            ZipFile(".DS_Store", "x"),
            ZipFile("__MACOSX/._封面.png", "x"),
        ]))
        directory = os.path.join(temp_dir("scratch-"), "zip-1")
        result = unpack_zip(archive, directory, refusal)
        self.assertEqual([f["name"] for f in result.files], ["素材.zip › 横版/GOFCN66367_片头.mp4", "素材.zip › 竖版/GOFCN66367_片头.mp4", "素材.zip › 封面.png"])
        self.assertTrue(all(os.path.dirname(f["file"]) == directory for f in result.files), "flat, in the folder given")
        with open(result.files[0]["file"], "rb") as handle:
            self.assertEqual(handle.read(), video, "deflated data comes back exactly")
        self.assertEqual(result.skipped, [
            {"file": "素材.zip › 说明.txt", "reason": "Not a video or an image: not reviewed"},
            {"file": "素材.zip › ../逃出去.mp4", "reason": "Unsafe path: not unpacked"},
        ])

    def test_zip64_archives_unpack_a_damaged_entry_or_too_many_materials_are_refused(self) -> None:
        ok = unpack_zip(zip_file("big.zip", make_zip([ZipFile("a.mp4", "aaaa")], zip64=True)), os.path.join(temp_dir("s-"), "z"), refusal)
        self.assertEqual(len(ok.files), 1)
        with self.assertRaisesRegex(Blocked, "is damaged"):
            unpack_zip(zip_file("bad.zip", make_zip([ZipFile("a.mp4", "aaaa", bad_crc=True)])), os.path.join(temp_dir("s-"), "z"), refusal)
        with self.assertRaisesRegex(Blocked, "Not a valid zip"):
            unpack_zip(zip_file("not.zip", b"hello"), os.path.join(temp_dir("s-"), "z"), refusal)
        many = make_zip([ZipFile(f"{i}.mp4", "x") for i in range(51)])
        with self.assertRaisesRegex(Blocked, "more than 50 materials"):
            unpack_zip(zip_file("many.zip", many), os.path.join(temp_dir("s-"), "z"), refusal)


if __name__ == "__main__":
    unittest.main()
