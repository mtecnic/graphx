"""Tiny functions used by examples/hello.yaml (and handy in smoke tests)."""

from __future__ import annotations

import platform
import sys


def sysinfo() -> dict:
    return {"platform": platform.platform(), "python": sys.version.split()[0]}


def split_words(text: str) -> dict:
    return {"words": str(text).split()}


def upper(word: str) -> str:
    return str(word).upper()


def increment(count: int) -> dict:
    return {"count": int(count) + 1}
