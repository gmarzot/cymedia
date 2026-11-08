"""ABR ladders: one branch per rung, scaled and encoded per vendor profile."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Iterable

from cymedia._core import Graph, have_encoder
from cymedia._types import Branch


@dataclass(frozen=True)
class Rung:
    width: int
    height: int
    bit_rate: int


def _rate(r: Rung, fps: tuple[int, int]) -> dict[str, str]:
    # CBR with a one-frame buffer caps keyframe bursts.
    return {"b": str(r.bit_rate), "maxrate": str(r.bit_rate),
            "bufsize": str(r.bit_rate * fps[1] // fps[0])}


def _nvenc(r: Rung, fps: tuple[int, int]) -> Branch:
    return Branch(
        filter=f"scale_cuda=w={r.width}:h={r.height}",
        encoder="h264_nvenc",
        options={"preset": "p1", "tune": "ull", "rc": "cbr", "bf": "0", "delay": "0",
                 "zerolatency": "1", "forced-idr": "1", "rc-lookahead": "0", "g": "100000",
                 **_rate(r, fps)},
        strip_filler=True)


def _cpu(r: Rung, fps: tuple[int, int]) -> Branch:
    if have_encoder("libopenh264"):
        enc, opts = "libopenh264", {}
    else:
        enc, opts = "libx264", {"preset": "veryfast", "tune": "zerolatency", "forced-idr": "1",
                                "x264-params": "scenecut=0"}
    return Branch(
        filter=f"scale=w={r.width}:h={r.height}:flags=bilinear,format=yuv420p",
        encoder=enc,
        options={"g": "100000", "bf": "0", **opts, **_rate(r, fps)})


PROFILES: dict[str, tuple[str | None, Callable[[Rung, tuple[int, int]], Branch]]] = {
    "nvenc": ("cuda", _nvenc),
    "cpu": (None, _cpu),
}


def ladder(rungs: Iterable[Rung], *, profile: str = "nvenc", fps: tuple[int, int] = (60, 1),
           **graph_kw) -> Graph:
    """A graph with one branch per rung, in order."""
    hwaccel, make = PROFILES[profile]
    return Graph([make(r, fps) for r in rungs], hwaccel=hwaccel, fps=fps, **graph_kw)
