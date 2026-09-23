#!/bin/zsh
# Offline synthetic simulator only. Does not start MT5 or submit trades.
set -eu

xau_project_dir="${0:A:h}"
cd -- "$xau_project_dir"

if ! command -v python3 >/dev/null 2>&1; then
    print -u2 "Python 3.9 or newer is required. Nothing has been installed or traded."
    exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    print -u2 "Python 3.9 or newer is required. The demo has not run."
    exit 1
fi

print "Starting the offline synthetic demonstration — no broker connection."
xau_exit_code=0
PYTHONPATH="$xau_project_dir/src" python3 -m xau_trader run-paper-demo || xau_exit_code=$?

if [[ -t 0 ]]; then
    print ""
    read -r "xau_reply?Press Enter to close this window. "
fi
exit "$xau_exit_code"
