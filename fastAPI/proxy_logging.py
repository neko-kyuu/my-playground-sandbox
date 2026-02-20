import logging
import os
from typing import Optional


_CONFIGURED = False


def setup_debug_logging(
    *,
    log_dir: str,
    filename: str = "debug.log",
    level: int = logging.DEBUG,
    also_console: bool = True,
) -> logging.Logger:
    """
    Configure a dedicated logger for this proxy.

    - Writes DEBUG logs into `log_dir/filename`.
    - Overwrites the file on each process start (mode='w', not append).
    - Optionally also logs to stderr for local debugging.
    """
    global _CONFIGURED

    logger = logging.getLogger("claude_to_openai_proxy")
    logger.setLevel(level)

    if _CONFIGURED:
        return logger

    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, filename)

    fmt = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    if also_console:
        stream_handler: Optional[logging.Handler] = logging.StreamHandler()
        stream_handler.setLevel(level)
        stream_handler.setFormatter(fmt)
        logger.addHandler(stream_handler)

    # Avoid double logging if root/uvicorn loggers propagate.
    logger.propagate = False

    _CONFIGURED = True
    return logger

