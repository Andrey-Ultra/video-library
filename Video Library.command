#!/bin/zsh
set -e
video_agent_dir="${0:A:h}"
cd "$video_agent_dir"
if [[ ! -x .venv/bin/python ]]; then
  print "Install the Python environment first. See README.md."
  read -r '?Press Enter to close.'
  exit 1
fi
exec .venv/bin/python main.py ui
