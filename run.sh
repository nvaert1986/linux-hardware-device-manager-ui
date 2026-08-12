#!/bin/bash
# Development runner. No venv, no pip -- straight from the source tree against system PyQt6.
cd "$(dirname "$0")"
exec python3 -m hardware_ui.shell "$@"
