#!/usr/bin/env bash
# Run graphx. Bootstraps the venv on first use, then forwards all args to the
# graphx CLI. With no args it launches the TUI.
#
#   ./run.sh                       # launch the TUI
#   ./run.sh examples/hello.yaml   # launch the TUI on a workflow
#   ./run.sh run examples/hello.yaml --input name=world
#   ./run.sh eval examples/hello.yaml examples/hello.eval.yaml
#   ./run.sh runs
set -euo pipefail

cd "$(dirname "$0")"

# First run (or a wiped venv): create it and install graphx editable.
if [[ ! -x venv/bin/graphx ]]; then
  echo "graphx: setting up venv (first run)..." >&2
  python3 -m venv venv
  ./venv/bin/pip install --quiet --upgrade pip
  # [server] pulls in the daemon/scheduler deps used by `serve` and triggers.
  ./venv/bin/pip install --quiet -e ".[server]"
fi

# No args → open the TUI. A single .yaml/.yml path → open the TUI on it.
# Anything else is a graphx subcommand, forwarded verbatim.
if [[ $# -eq 0 ]]; then
  exec ./venv/bin/graphx tui
elif [[ $# -eq 1 && ( "$1" == *.yaml || "$1" == *.yml ) ]]; then
  exec ./venv/bin/graphx tui "$1"
else
  exec ./venv/bin/graphx "$@"
fi
