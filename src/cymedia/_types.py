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


class Packet(NamedTuple):
    """One packet from a Source; stream is "video" or "audio"."""

    stream: str
    group: int
    object: int
    pts_us: int
    key: bool
    data: bytes


@dataclass(frozen=True)
class StreamInfo:
    """A Source's stream as probed. extradata is the decoder configuration (avcC for H.264);
    length_prefixed video carries 4-byte NAL unit lengths instead of start codes."""

    codec: str
    extradata: bytes | None = None
    length_prefixed: bool = False
    width: int = 0
    height: int = 0
    fps: tuple[int, int] | None = None
    sample_rate: int = 0
    channels: int = 0
    bit_rate: int = 0


@dataclass(frozen=True)
class Branch:
    """A libavfilter chain (empty passes frames through) and the encoder after it.
    strip_filler drops H.264 filler NAL units and AV1 padding OBUs from the output."""

    filter: str
    encoder: str
    options: Mapping[str, str] = field(default_factory=dict)
    strip_filler: bool = False
