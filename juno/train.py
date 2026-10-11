"""Train Llama X JUNO with mlx-lm, and resume from the latest checkpoint.

mlx-lm can reload adapter weights, but on its own a "resume" starts over in
every other way: the step count and learning-rate schedule go back to step 1,
the data order replays from the top, and new checkpoints are numbered from 100
again (overwriting the real ones). This wrapper makes a resume continue the run:

  - the optimizer's step counter is set to the checkpoint's step, so the
    learning-rate schedule (warmup + cosine) carries on from that exact point
  - the first <step> training batches are skipped, so it picks up the same
    shuffled order where it stopped (same seed, same data, same batch size)
  - checkpoints, progress lines and saves use the real step numbers
  - Adam's running averages are saved next to each checkpoint from now on and
    restored on resume (checkpoints made before this existed have none, so
    those resume with fresh averages)

Usage (via juno/train.sh):
    train.sh            resume from the latest checkpoint, or start if there is none
    train.sh --fresh    discard old checkpoints and start over
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import shutil
import sys
from pathlib import Path

import mlx.core as mx
import mlx.optimizers as optim
import yaml
from mlx.utils import tree_flatten

import mlx_lm.cli_ui as cli_ui
import mlx_lm.lora as lora
import mlx_lm.tuner.trainer as trainer

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("JUNO_HOME", Path.home() / ".faam" / "juno"))
BASE = os.environ.get("JUNO_BASE_MODEL", "mlx-community/Meta-Llama-3.1-8B-Instruct-4bit")
CKPT = re.compile(r"^(\d{7})_adapters\.safetensors$")
# Settings that must match for a checkpoint to be resumable.
MUST_MATCH = ("model", "fine_tune_type", "num_layers", "batch_size", "learning_rate", "lr_schedule",
              "lora_parameters", "max_seq_length", "mask_prompt", "seed", "optimizer")

START = 0            # the step being resumed from (0 = fresh run)
ADAPTERS: Path = HOME / "adapters"
_optimizers: list = []


def latest_checkpoint(adapters: Path) -> tuple:
    """(step, path) of the newest NNNNNNN_adapters.safetensors, or (0, None)."""
    best = (0, None)
    if adapters.is_dir():
        for p in adapters.iterdir():
            m = CKPT.match(p.name)
            if m and int(m.group(1)) > best[0]:
                best = (int(m.group(1)), p)
    return best


def mismatches(saved: dict, wanted: dict) -> list:
    return [k for k in MUST_MATCH if k in saved and k in wanted and saved[k] != wanted[k]]


def optimizer_file(step: int) -> Path:
    return ADAPTERS / f"{step:07d}_optimizer.safetensors"


def _set_in(tree, dotted: str, value) -> None:
    """Assign into the optimizer's state tree in place. The compiled training
    step holds a reference to that tree, so it must not be replaced."""
    parts = dotted.split(".")
    node = tree
    for p in parts[:-1]:
        node = node[int(p)] if isinstance(node, list) else node[p]
    if isinstance(node, list):
        node[int(parts[-1])] = value
    else:
        node[parts[-1]] = value


# ---- hooks into mlx-lm -------------------------------------------------------

_orig_train = trainer.train
_orig_iterate = trainer.iterate_batches
_orig_save = mx.save_safetensors


def _iterate(*a, **k):
    """Skip the batches the earlier run already trained on (training stream only;
    validation batches are left alone)."""
    gen = _orig_iterate(*a, **k)
    return itertools.islice(gen, START, None) if (START and k.get("loop")) else gen


def _train(*a, **k):
    names = ("model", "optimizer", "train_dataset", "val_dataset", "args")
    k.update(dict(zip(names, a)))
    model, optimizer = k["model"], k["optimizer"]
    _optimizers[:] = [optimizer]
    if START:
        optimizer.init(model.trainable_parameters())
        optimizer.state["step"] = mx.array(START, dtype=mx.uint64)
        restored = 0
        saved = optimizer_file(START)
        if saved.exists():
            arrays = mx.load(str(saved))
            current = dict(tree_flatten(optimizer.state))
            for key, value in arrays.items():
                if key in current and key not in ("step", "learning_rate") and value.shape == current[key].shape:
                    _set_in(optimizer.state, key, value)
                    restored += 1
        mx.eval(optimizer.state)
        sched = getattr(optimizer, "_schedulers", {}).get("learning_rate")
        lr_next = sched(optimizer.step).item() if sched else float(optimizer.learning_rate)
        print(f"Resuming at step {START}: the next update uses learning rate {lr_next:.3g}, "
              f"continuing the schedule; "
              + (f"restored {restored} optimizer averages." if restored else
                 "no saved optimizer averages for this checkpoint, so Adam starts them fresh."),
              flush=True)
    k.setdefault("iterate_batches", _iterate)
    return _orig_train(**k)


def _save(path, arrays, *a, **k):
    """Name checkpoints by their real step, and save Adam's averages beside them."""
    p = Path(str(path))
    m = CKPT.match(p.name)
    if not m or p.parent.resolve() != ADAPTERS.resolve():
        return _orig_save(path, arrays, *a, **k)
    step = START + int(m.group(1))
    out = p.with_name(f"{step:07d}_adapters.safetensors")
    _orig_save(str(out), arrays, *a, **k)
    if _optimizers:
        state = {key: v for key, v in tree_flatten(_optimizers[0].state)
                 if key not in ("step", "learning_rate") and isinstance(v, mx.array)}
        if state:
            _orig_save(str(optimizer_file(step)), state)


def _shift(method):
    def wrapped(self, it, *a, **k):
        return method(self, it + START, *a, **k)
    return wrapped


_orig_report_save = cli_ui.TrainUI.report_save


def _report_save(self, checkpoint):
    m = CKPT.match(Path(str(checkpoint)).name)
    if m:
        checkpoint = Path(str(checkpoint)).with_name(f"{START + int(m.group(1)):07d}_adapters.safetensors")
    return _orig_report_save(self, checkpoint)


def install_hooks() -> None:
    trainer.train = _train
    lora.train = _train
    mx.save_safetensors = _save
    cli_ui.TrainUI.report_train = _shift(cli_ui.TrainUI.report_train)
    cli_ui.TrainUI.report_val = _shift(cli_ui.TrainUI.report_val)
    cli_ui.TrainUI.report_save = _report_save


# ---- entry point -------------------------------------------------------------

def main(argv=None) -> int:
    global START, ADAPTERS
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fresh", action="store_true", help="discard checkpoints and start over")
    ap.add_argument("--config", default=str(HERE / "lora.yaml"))
    ap.add_argument("--adapters", default=str(HOME / "adapters"))
    ap.add_argument("--data", default=str(HOME / "data"))
    args = ap.parse_args(argv)

    ADAPTERS = Path(args.adapters)
    cfg = yaml.safe_load(Path(args.config).read_text())
    total = int(cfg["iters"])
    wanted = {**cfg, "model": BASE, "optimizer": cfg.get("optimizer", "adam")}

    if args.fresh and ADAPTERS.exists():
        shutil.rmtree(ADAPTERS)
        print("Starting fresh: removed the old checkpoints.", flush=True)
    step, ckpt = latest_checkpoint(ADAPTERS)
    if step:
        saved_cfg = {}
        try:
            saved_cfg = json.loads((ADAPTERS / "adapter_config.json").read_text())
        except (OSError, ValueError):
            pass
        bad = mismatches(saved_cfg, wanted)
        if bad:
            print(f"Can't resume: the checkpoints were trained with different {', '.join(bad)}. "
                  "Run with --fresh to start over.", file=sys.stderr)
            return 2
        if step >= total:
            print(f"Already trained: checkpoint {step} of {total}. Nothing to do.", flush=True)
            return 0
        START = step
        lr_now = lora.build_schedule(cfg["lr_schedule"])(mx.array(step)).item() if cfg.get("lr_schedule") else cfg["learning_rate"]
        print(f"Found checkpoint {step} of {total}; resuming with {total - step} steps to go "
              f"(learning rate now {lr_now:.3g}).", flush=True)

    install_hooks()
    cli = ["-c", args.config, "--model", BASE, "--train", "--data", args.data,
           "--adapter-path", str(ADAPTERS), "--iters", str(total - START)]
    if START:
        cli += ["--resume-adapter-file", str(ckpt)]
    sys.argv = ["mlx_lm.lora"] + cli
    lora.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
