#!/bin/bash
# Double-click to launch Token Tracker (add --demo to preview with fake data)
cd "$(dirname "$0")" && exec python3 token_meter.py "$@"
