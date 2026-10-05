#!/usr/bin/env bash
set -euo pipefail
task_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$task_root"
export PYTHONPATH="$task_root${PYTHONPATH:+:$PYTHONPATH}"
export GRC_BLOCKS_PATH="$task_root/grc${GRC_BLOCKS_PATH:+:$GRC_BLOCKS_PATH}"
exec gnuradio-companion "$task_root/StageO_IQ_Replay.grc"
