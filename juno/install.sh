#!/bin/bash
# Put JUNO's server where FAAM looks for it (~/.faam/juno/app). FAAM starts it
# on its own the first time someone picks Llama X JUNO as their forecast model.
set -euo pipefail
JUNO_HOME="${JUNO_HOME:-$HOME/.faam/juno}"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$JUNO_HOME/app"
cp "$HERE/serve.py" "$HERE/runtime.py" "$JUNO_HOME/app/"
cp "$HERE/LICENSE-LLAMA.txt" "$JUNO_HOME/" 2>/dev/null || true
echo "Installed Llama X JUNO's server to $JUNO_HOME/app"
if [ -f "$JUNO_HOME/adapters/adapters.safetensors" ] && ! pgrep -f "mlx_lm lora" >/dev/null; then
  # The marker FAAM looks for before it offers JUNO anywhere.
  printf '{"name": "Llama X JUNO", "installed": "%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$JUNO_HOME/installed.json"
  echo "Llama X JUNO is ready — FAAM will offer it."
else
  rm -f "$JUNO_HOME/installed.json"
  echo "Not ready yet: train JUNO first (juno/train.sh), and let training finish."
fi
