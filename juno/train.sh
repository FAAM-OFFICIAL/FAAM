#!/bin/bash
# Fine-tune Llama 3.1 8B Instruct into Llama X JUNO with LoRA on Apple silicon.
#   juno/train.sh           resume from the latest checkpoint (or start, if none)
#   juno/train.sh --fresh   discard checkpoints and start over
# Settings live in juno/lora.yaml; juno/train.py makes a resume continue the run
# (step count, learning-rate schedule, data order, checkpoint numbering).
# About 3.5 hours for 600 steps on an M4 with 24 GB; close memory-hungry apps
# first, because training slows to a crawl when the Mac swaps. Weights and
# checkpoints stay in ~/.faam/juno (never in the repo). Run build_data.py first.
set -euo pipefail
JUNO_HOME="${JUNO_HOME:-$HOME/.faam/juno}"
HERE="$(cd "$(dirname "$0")" && pwd)"
rm -f "$JUNO_HOME/installed.json"          # FAAM stops offering JUNO while it trains
exec "$JUNO_HOME/venv/bin/python" "$HERE/train.py" "$@"
