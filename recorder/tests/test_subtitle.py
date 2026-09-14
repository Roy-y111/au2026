"""字幕流程的純函式測試（不打網路、不叫 agent）：python -m unittest discover -s tests"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from au2026rec import subtitle  # noqa: E402
from au2026rec.subtitle import (  # noqa: E402
    SubtitleError,
    assemble_and_verify,
    cues_from_segments,
    extract_json_array,
    format_ts,
    make_batches,
    parse_srt,
    title_from_filename,
    translate_batch,
    write_srt,
)


class TimecodeTests(unittest.TestCase):
    def test_format(self):
        self.assertEqual(format_ts(0), "00:00:00,000")
        self.assertEqual(format_ts(3661.5), "01:01:01,500")
        self.assertEqual(format_ts(599.9996), "00:10:00,000")

    def test_offset_and_filter(self):
        segs = [
            {"start": 0.0, "end": 2.0, "text": " Hello everyone."},
            {"start": 2.0, "end": 3.0, "text": "Thanks for watching!"},
            {"start": 3.0, "end": 4.0, "text": "Key terms: Revit, Forma"},
            {"start": 4.0, "end": 6.0, "text": "Let's talk about MCP."},
        ]
        cues = cues_from_segments(segs, offset=600)
        self.assertEqual([c.text for c in cues], ["Hello everyone.", "Let's talk about MCP."])
        self.assertEqual(cues[0].start, 600.0)


class SrtRoundTripTests(unittest.TestCase):
    def test_write_parse(self):
        entries = [("00:00:01,000 --> 00:00:02,000", "One"),
                   ("00:00:02,500 --> 00:00:04,000", "Two")]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "a.srt")
            write_srt(p, entries)
            self.assertEqual(parse_srt(p.read_text(encoding="utf-8")), entries)

    def test_assemble_keeps_timecodes(self):
        en = [("00:00:01,000 --> 00:00:02,000", "One"), ("00:00:03,000 --> 00:00:04,000", "Two")]
        out = assemble_and_verify(en, ["一", "二"])
        self.assertEqual([tc for tc, _ in out], [tc for tc, _ in en])

    def test_assemble_rejects_mismatch(self):
        en = [("00:00:01,000 --> 00:00:02,000", "One")]
        with self.assertRaises(SubtitleError):
            assemble_and_verify(en, ["一", "二"])
        with self.assertRaises(SubtitleError):
            assemble_and_verify(en, [" "])


class BatchTests(unittest.TestCase):
    def test_limits(self):
        texts = ["x" * 10] * 95
        batches = make_batches(texts, max_cues=40, max_chars=10_000)
        self.assertEqual([len(b) for b in batches], [40, 40, 15])
        self.assertEqual(sum(batches, []), list(range(95)))
        batches = make_batches(["x" * 30] * 5, max_cues=40, max_chars=65)
        self.assertEqual([len(b) for b in batches], [2, 2, 1])

    def test_extract_json(self):
        self.assertEqual(extract_json_array('好的：\n```json\n["一", "二"]\n```'), ["一", "二"])
        with self.assertRaises(ValueError):
            extract_json_array("沒有陣列")

    def test_translate_splits_when_misaligned(self):
        calls = []

        def fake_agent(prompt, translator, model):
            n = prompt.count('"en"')
            calls.append(n)
            if n > 2:  # 大批故意少回一句，逼它拆批
                return json.dumps(["x"] * (n - 1))
            return json.dumps([f"譯{i}" for i in range(n)], ensure_ascii=False)

        subtitle.time.sleep = lambda s: None
        out = translate_batch(["a", "b", "c", "d"], "t", "", "agy", "m", lambda m: None, fake_agent)
        self.assertEqual(len(out), 4)
        self.assertEqual(calls[:3], [4, 4, 4])  # 先重試 3 次，才拆成 2+2


class FilenameTests(unittest.TestCase):
    def test_title(self):
        self.assertEqual(title_from_filename(Path("20260916_0000_KEY1001-D_Day 1 Keynote.mkv")),
                         "KEY1001-D_Day 1 Keynote".replace("_", " "))
        self.assertEqual(title_from_filename(Path("my talk.mp4")), "my talk")


if __name__ == "__main__":
    unittest.main()
