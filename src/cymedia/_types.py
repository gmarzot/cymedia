from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import NamedTuple


class Output(NamedTuple):
    """One encoded frame from a branch, carrying the source frame's identity."""

    branch: int
    group: int
    object: int
    pts_us: int
    key: bool
    group_start: bool
    data: bytes


@dataclass(frozen=True)
class Branch:
    """A libavfilter chain (empty passes frames through) and the encoder after it."""

    filter: str
    encoder: str
    options: Mapping[str, str] = field(default_factory=dict)
    strip_filler: bool = False
