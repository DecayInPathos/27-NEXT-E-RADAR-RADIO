#!/usr/bin/env bash
set -euo pipefail
task_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [[ $# -ne 1 ]]; then
  echo 'Usage: bash validate_gnu_on_host.sh /absolute/path/to/recording.c64' >&2
  exit 2
fi
iq_path=$(realpath -- "$1")
cd "$task_root"
python3 -c 'import gnuradio, numpy, scipy'
python3 stage_o_patched_rx.py --iq "$iq_path" --output host_validation/standalone
python3 stage_o_gr_replay.py --iq "$iq_path" --output host_validation/gnuradio \
  --reference-summary host_validation/standalone/summary.json --verify-traces
