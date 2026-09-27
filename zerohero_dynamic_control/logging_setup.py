"""Rich logging configuration."""

from __future__ import annotations

import logging

from rich.console import Console
from rich.logging import RichHandler

from .config import LoggingConfig


def setup_logging(cfg: LoggingConfig | None = None, *, level: str | None = None) -> Console:
    cfg = cfg or LoggingConfig()
    console = Console(stderr=False)
    logging.basicConfig(
        level=(level or cfg.level).upper(),
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            RichHandler(
                console=console,
                rich_tracebacks=cfg.rich_tracebacks,
                show_path=False,
                omit_repeated_times=False,
            )
        ],
        force=True,
    )
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return console
