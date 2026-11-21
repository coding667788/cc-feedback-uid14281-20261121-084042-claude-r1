"""``python -m solution`` 入口：仅转发到 :mod:`solution.cli`。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
