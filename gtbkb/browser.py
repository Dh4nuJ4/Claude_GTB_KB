"""Playwright helpers shared by login and crawl."""

from __future__ import annotations

import os
from typing import Any


def launch(p, headless: bool):
    """Launch Chromium. KB_CHROMIUM_PATH points at a specific browser binary if needed."""
    kwargs: dict[str, Any] = {"headless": headless}
    exe = os.environ.get("KB_CHROMIUM_PATH")
    if exe:
        kwargs["executable_path"] = exe
    return p.chromium.launch(**kwargs)
