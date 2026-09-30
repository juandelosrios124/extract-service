"""Logging setup.

Follows 12-Factor App (XI. Logs): the service never manages log files, it
writes an unbuffered event stream to stdout and the environment routes it.
"""

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s - %(message)s"


def configure_logging(level: str) -> None:
    """Send every log record (including uvicorn's) to stdout at ``level``."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
