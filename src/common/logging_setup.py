"""统一的日志配置，便于在云/边/诊断器多组件中共用同一套格式。"""
from __future__ import annotations

import logging
import sys

_COLORS = {
    "cloud": "\033[36m",   # cyan
    "edge": "\033[32m",    # green
    "agent": "\033[35m",   # magenta
    "demo": "\033[33m",    # yellow
    "reset": "\033[0m",
}


def get_logger(name: str, component: str = "cloud", level: int = logging.INFO) -> logging.Logger:
    """返回带组件前缀与配色（终端）的 logger。

    Args:
        name: logger 名称（通常为模块名）。
        component: 逻辑组件名，用于配色与标识（cloud/edge/agent/demo）。
    """
    logger = logging.getLogger(f"edgemind.{name}")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    color = _COLORS.get(component, "")
    reset = _COLORS["reset"]
    fmt = (
        f"{color}%(asctime)s [%(levelname).1s] {component:<5}{reset} "
        "%(name)s: %(message)s"
    )
    handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger
