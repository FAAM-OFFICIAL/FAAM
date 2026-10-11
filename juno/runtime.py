"""Load Llama X JUNO and read its band probabilities. Shared by serve.py and
evaluate.py. Runs in the JUNO environment (~/.faam/juno/venv) on Apple silicon."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import mlx.core as mx
from mlx_lm import generate, load
from mlx_lm.sample_utils import make_sampler

HOME = Path(os.environ.get("JUNO_HOME", Path.home() / ".faam" / "juno"))
BASE = os.environ.get("JUNO_BASE_MODEL", "mlx-community/Meta-Llama-3.1-8B-Instruct-4bit")
BANDS = ("A", "B", "C", "D", "E")


class Juno:
    def __init__(self, adapters: bool = True):
        path = HOME / "adapters"
        self.tuned = adapters and (path / "adapters.safetensors").exists()
        self.model, self.tok = load(BASE, adapter_path=str(path) if self.tuned else None)
        self.meta = json.loads((HOME / "data" / "meta.json").read_text())

    def _prefix(self, system: str, user: str) -> str:
        return self.tok.apply_chat_template(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            add_generation_prompt=True, tokenize=False)

    def band_probs(self, prompt: str) -> dict:
        """P(band) for A..E, read straight from the model's next-token odds."""
        prefix = self._prefix(self.meta["forecastSystem"], prompt)
        ids = [self.tok.encode(prefix + json.dumps({"band": b}), add_special_tokens=False) for b in BANDS]
        n = 0
        while all(len(x) > n for x in ids) and len({x[n] for x in ids}) == 1:
            n += 1
        nxt = [x[n] if len(x) > n else None for x in ids]
        if None not in nxt and len(set(nxt)) == len(BANDS):
            logits = self._last_logits(ids[0][:n])
            lp = logits - mx.logsumexp(logits)
            scores = [lp[t].item() for t in nxt]
        else:                               # letters didn't split cleanly: score whole answers
            scores = [self._seq_logprob(x, n) for x in ids]
        top = max(scores)
        e = [math.exp(s - top) for s in scores]
        z = sum(e)
        return {b: e[i] / z for i, b in enumerate(BANDS)}

    def _last_logits(self, ids: list):
        """Next-token logits at the end only: running the vocabulary projection
        over every position (128k words x ~400 tokens) costs more than the model."""
        hidden = self.model.model(mx.array([ids]))[:, -1:, :]
        if getattr(self.model.args, "tie_word_embeddings", False):
            out = self.model.model.embed_tokens.as_linear(hidden)
        else:
            out = self.model.lm_head(hidden)
        return out[0, -1].astype(mx.float32)

    def _seq_logprob(self, ids: list, start: int) -> float:
        logits = self.model(mx.array([ids[:-1]]))[0].astype(mx.float32)
        lp = logits - mx.logsumexp(logits, axis=-1, keepdims=True)
        return sum(lp[i - 1, ids[i]].item() for i in range(start, len(ids)))

    def chat(self, messages: list, max_tokens: int = 300) -> str:
        msgs = messages if messages and messages[0].get("role") == "system" else \
            [{"role": "system", "content": self.meta["explainSystem"]}] + messages
        prompt = self.tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
        return generate(self.model, self.tok, prompt=prompt, max_tokens=max_tokens,
                        sampler=make_sampler(temp=0.3), verbose=False).strip()
