import os
import subprocess
import unittest

from audit_sdk.errors import Blocked
from audit_sdk.media import INLINE_MAX, Probe, find_ffmpeg, fps_for, kind_of, prepare, probe, refusal

from tests.helpers.mp4 import make_mp4
from tests.helpers.tmp import temp_dir


def file(name: str, data: bytes | str) -> str:
    at = os.path.join(temp_dir("media-"), name)
    with open(at, "wb") as handle:
        handle.write(data.encode("utf-8") if isinstance(data, str) else data)
    return at


def truncate(path: str, size: int) -> None:
    with open(path, "r+b") as handle:
        handle.truncate(size)


FFMPEG = find_ffmpeg()


class MediaTest(unittest.TestCase):
    def test_duration_and_the_picture_as_shown_come_from_the_mp4_header_wherever_the_movie_box_is(self) -> None:
        self.assertEqual(probe(file("a.mp4", make_mp4(10.77, 1080, 1920))), Probe(10.77, 1080, 1920))
        self.assertEqual(probe(file("b.mp4", make_mp4(37.61, 1920, 1080, moov_first=True, version1=True))), Probe(37.61, 1920, 1080))
        # A phone video stored on its side, rotated by the matrix: shown upright.
        self.assertEqual(probe(file("c.mov", make_mp4(6.25, 1920, 1080, rotated=True))), Probe(6.25, 1080, 1920))
        self.assertEqual(probe(file("d.mp4", "not a video at all")), Probe())
        self.assertEqual(probe(file("e.mp4", make_mp4(5, 720, 1280)[:60])), Probe(), "a cut-off file says nothing")
        whole = probe(file("f.mp4", make_mp4(11, 1080, 1920)))
        self.assertIsInstance(whole.duration, int, "a whole number of seconds is written as 11, not 11.0")

    def test_every_video_is_looked_at_two_frames_a_second(self) -> None:
        self.assertEqual([fps_for(6), fps_for(300), fps_for(None)], [2, 2, 2])

    def test_only_videos_and_images_are_material_gifs_and_other_files_are_refused_with_the_reason(self) -> None:
        self.assertIsNone(refusal("GOFCN1_竖版.MP4"))
        self.assertIsNone(refusal("海报.jpeg"))
        self.assertRegex(refusal("动图.gif") or "", "GIF")
        self.assertRegex(refusal("脚本.docx") or "", "不是视频或图片")
        self.assertEqual(kind_of("a.mov"), "video")
        self.assertEqual(kind_of("a.webp"), "image")
        self.assertIsNone(kind_of("a.txt"))

    def test_without_ffmpeg_a_video_goes_as_it_is_an_mkv_is_refused_and_one_over_the_inline_limit_too(self) -> None:
        scratch = temp_dir("scratch-")
        small = prepare(file("竖版.mp4", make_mp4(30, 1080, 1920)), scratch, None)
        self.assertEqual((small.compressed, small.orientation, small.fps, small.mime), (False, "竖版", 2, "video/mp4"))
        self.assertEqual(prepare(file("横版.mov", make_mp4(90, 1920, 1080)), scratch, None).mime, "video/mov")
        with self.assertRaisesRegex(Blocked, "转成 MP4"):
            prepare(file("a.mkv", "x"), scratch, None)
        big = file("big.mp4", "")
        truncate(big, INLINE_MAX + 1)
        with self.assertRaisesRegex(Blocked, r"视频太大（244 MB）"):
            prepare(big, scratch, None)
        with self.assertRaisesRegex(Blocked, "空的"):
            prepare(file("empty.mp4", ""), scratch, None)

    @unittest.skipUnless(FFMPEG, "no ffmpeg on this computer")
    def test_a_big_video_is_compressed_to_720p_24fps_with_ffmpeg_when_the_computer_has_it(self) -> None:
        assert FFMPEG
        scratch = temp_dir("scratch-")
        source = os.path.join(temp_dir("media-"), "竖版原片.mp4")
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30", "-f", "lavfi",
                        "-i", "sine=frequency=440", "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
                        "-movflags", "+faststart", source], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        # Padded past the inline limit, above which a video is compressed (the demuxer stops at the end of the movie).
        truncate(source, INLINE_MAX + 1024)
        done = prepare(source, scratch, FFMPEG)
        self.assertTrue(done.compressed)
        self.assertTrue(done.file.startswith(scratch), "the copy is in the scratch folder, never next to the original")
        self.assertEqual((done.mime, done.orientation), ("video/mp4", "竖版"))
        self.assertLess(done.sent, 2 * 1024 * 1024)
        shown = probe(done.file)
        self.assertEqual((shown.width, shown.height), (720, 1280))

    def test_when_compressing_a_video_over_the_limit_fails_it_is_refused(self) -> None:
        big = file("big.mp4", make_mp4(20, 1080, 1920, moov_first=True))
        truncate(big, INLINE_MAX + 1024)
        with self.assertRaisesRegex(Blocked, "转成 720p 也失败了"):
            prepare(big, temp_dir("scratch-"), os.path.join(temp_dir("bin-"), "no-such-ffmpeg.exe"))

if __name__ == "__main__":
    unittest.main()
