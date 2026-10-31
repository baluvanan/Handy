"""Tests for run_eval.py: python3 -m unittest discover -s eval"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run_eval  # noqa: E402

HERE = Path(__file__).parent
FAKE = f"{sys.executable} {HERE / 'fake_handy.py'}"


class NormalizeTests(unittest.TestCase):
    def test_case_punctuation_and_numbers(self):
        self.assertEqual(
            run_eval.normalize("Revenue grew by 12%, and we hired four!"),
            ["revenue", "grew", "by", "12", "%", "and", "we", "hired", "4"],
        )
        self.assertEqual(
            run_eval.normalize("twelve percent"), run_eval.normalize("12%")
        )

    def test_fillers_only_dropped_when_asked(self):
        self.assertEqual(run_eval.normalize("um so uh yes"), ["um", "so", "uh", "yes"])
        self.assertEqual(run_eval.normalize("um so uh yes", ignore_fillers=True), ["so", "yes"])


class WerTests(unittest.TestCase):
    def test_identical_is_zero(self):
        self.assertEqual(run_eval.wer("Hello there.", "hello there")[0], 0.0)

    def test_sub_del_ins(self):
        ref = "the cat sat on the mat".split()
        self.assertEqual(run_eval.edit_distance(ref, "the cat sit on the mat".split()), (1, 0, 0))
        self.assertEqual(run_eval.edit_distance(ref, "the cat on the mat".split()), (0, 1, 0))
        self.assertEqual(run_eval.edit_distance(ref, "the big cat sat on the mat".split()), (0, 0, 1))
        w, errors, n = run_eval.wer("the cat sat on the mat", "a cat on the mat today")
        self.assertEqual((errors, n), (3, 6))
        self.assertAlmostEqual(w, 0.5)

    def test_empty_reference(self):
        self.assertEqual(run_eval.wer("", "")[0], 0.0)
        self.assertEqual(run_eval.wer("", "noise")[0], 1.0)


class EndToEndTests(unittest.TestCase):
    """Runs the real pipeline over eval/samples with a fake Handy."""

    def run_main(self, mode, *extra):
        os.environ["FAKE_HANDY_MODE"] = mode
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_eval.main(["--handy", FAKE, "--models", "m1,m2", *extra])
        return code, buf.getvalue()

    def test_samples_have_references(self):
        clips = run_eval.discover_clips(HERE / "samples")
        self.assertGreaterEqual(len(clips), 3)

    def test_perfect_transcriber_scores_zero(self):
        code, out = self.run_main("perfect", "--max-wer", "0")
        self.assertEqual(code, 0, out)
        self.assertIn("m1", out)
        self.assertIn("0.0%", out)

    def test_noisy_transcriber_fails_the_gate(self):
        code, out = self.run_main("noisy", "--max-wer", "0.05")
        self.assertEqual(code, 1, out)

    def test_failing_binary_counts_as_full_error(self):
        code, out = self.run_main("fail", "--max-wer", "0.5")
        self.assertEqual(code, 1)
        self.assertIn("   3    3", out)  # 3 clips, 3 failed

    def tearDown(self):
        os.environ.pop("FAKE_HANDY_MODE", None)


if __name__ == "__main__":
    unittest.main()
