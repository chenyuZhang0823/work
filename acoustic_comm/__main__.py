"""支持 uv run python -m acoustic_comm。"""

from .cli import entrypoint

raise SystemExit(entrypoint())
