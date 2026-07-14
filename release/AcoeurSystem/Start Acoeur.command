#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
./scripts/start.sh
echo
read -n 1 -s -r -p "Press any key to close this window..."
