"""Tests for juno/train.py's resume logic. Needs MLX, so run it with JUNO's Python:

    ~/.faam/juno/venv/bin/python -m unittest juno/test_train.py -v
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mlx.core as mx  # noqa: E402

import train as t  # noqa: E402


class TestResume(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="juno-train-"))
        self.old = (t.START, t.ADAPTERS, list(t._optimizers))
        t.ADAPTERS = self.dir

    def tearDown(self):
        t.START, t.ADAPTERS, t._optimizers[:] = self.old[0], self.old[1], self.old[2]
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_latest_checkpoint(self):
        self.assertEqual(t.latest_checkpoint(self.dir), (0, None))
        for name in ("0000100_adapters.safetensors", "0000400_adapters.safetensors",
                     "0000300_adapters.safetensors", "adapters.safetensors", "0000400_optimizer.safetensors"):
            (self.dir / name).write_bytes(b"x")
        step, path = t.latest_checkpoint(self.dir)
        self.assertEqual((step, path.name), (400, "0000400_adapters.safetensors"))

    def test_settings_must_match(self):
        saved = {"num_layers": 8, "batch_size": 2, "iters": 600, "lr_schedule": {"warmup": 40}}
        self.assertEqual(t.mismatches(saved, {"num_layers": 8, "batch_size": 2, "iters": 200,
                                              "lr_schedule": {"warmup": 40}}), [], "iters may change")
        self.assertEqual(t.mismatches(saved, {"num_layers": 16, "batch_size": 2}), ["num_layers"])

    def test_set_in_place(self):
        tree = {"step": 0, "layers": [{"m": 1}, {"m": 2}]}
        ref = tree["layers"]
        t._set_in(tree, "layers.1.m", 9)
        self.assertEqual(tree["layers"][1]["m"], 9)
        self.assertIs(tree["layers"], ref, "no new containers: the compiled step keeps its references")

    def test_skips_only_the_training_stream(self):
        t._orig_iterate, keep = (lambda *a, **k: iter(range(10))), t._orig_iterate
        try:
            t.START = 4
            self.assertEqual(list(t._iterate(loop=True))[:2], [4, 5])
            self.assertEqual(list(t._iterate(loop=False))[:2], [0, 1], "validation isn't skipped")
            t.START = 0
            self.assertEqual(list(t._iterate(loop=True))[:1], [0])
        finally:
            t._orig_iterate = keep

    def test_checkpoints_use_real_steps_and_keep_optimizer_state(self):
        class FakeOpt:
            state = {"step": mx.array(7), "learning_rate": mx.array(0.1),
                     "lora": {"m": mx.ones((2,)), "v": mx.zeros((2,))}}
        t.START = 400
        t._optimizers[:] = [FakeOpt()]
        t._save(self.dir / "0000100_adapters.safetensors", {"w": mx.zeros((3,))})
        names = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(names, ["0000500_adapters.safetensors", "0000500_optimizer.safetensors"])
        saved = mx.load(str(self.dir / "0000500_optimizer.safetensors"))
        self.assertEqual(sorted(saved), ["lora.m", "lora.v"], "step and learning rate aren't saved")
        t._save(self.dir / "adapters.safetensors", {"w": mx.zeros((3,))})
        self.assertTrue((self.dir / "adapters.safetensors").exists(), "other saves pass straight through")


if __name__ == "__main__":
    unittest.main(verbosity=2)
