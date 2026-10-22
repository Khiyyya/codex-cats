#!/bin/bash
cd "$(dirname "$0")" || exit 1
/bin/bash scripts/macos.sh install
cats_status=$?
if [[ -t 0 ]]; then read -r -p "Press Return to close..."; fi
exit "$cats_status"
