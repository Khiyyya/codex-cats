#!/bin/bash
# Source-based macOS preview: a private venv, never the system site-packages.
set -euo pipefail
umask 077

cats_project="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cats_action="${1:-install}"
cats_home="${CATS_HOME:-$HOME/Library/Application Support/CodexCATS}"
cats_runtime="$cats_home/runtime"
cats_python="$cats_runtime/bin/python"
if [[ "$cats_home" != /* ]]; then
    echo 'CATS_HOME must be an absolute path.' >&2
    exit 2
fi

if [[ "$(uname -s)" != Darwin ]]; then
    echo 'This entry point requires macOS.' >&2
    exit 1
fi
case "$cats_action" in
    install|start|open|stop|uninstall|status|doctor) ;;
    *) echo 'Usage: macos.sh {install|start|open|stop|uninstall|status|doctor}' >&2; exit 2 ;;
esac

if [[ ! -f "$cats_runtime/.cats-ready" ]]; then
    case "$cats_action" in
        stop|uninstall|status|doctor)
            echo 'CATS runtime is not installed. Run INSTALL-MAC.command first.'
            exit 0
            ;;
    esac
    cats_bootstrap="${CATS_PYTHON:-python3}"
    if ! "$cats_bootstrap" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
        echo 'Python 3.11+ is required. Set CATS_PYTHON to a stable Python executable.' >&2
        exit 1
    fi
    # A failed download leaves an unactivated runtime that can be retried.
    if [[ ! -x "$cats_python" ]]; then
        "$cats_bootstrap" -m venv "$cats_runtime"
    fi
    "$cats_python" -m pip install --no-cache-dir "$cats_project"
    "$cats_python" -c 'import aiohttp, websockets, zstandard, tomlkit, uplink12.macos'
    touch "$cats_runtime/.cats-ready"
fi

# Do not change cwd to the project: the installed package works after it is moved.
cd "$cats_home"
exec "$cats_python" -m uplink12.desktop --home "$cats_home" "$cats_action"
