"""ABR ladders: one branch per rung, scaled and encoded per vendor profile and codec."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Iterable

from cymedia._core import Graph, Source, have_encoder
from cymedia._types import Branch


@dataclass(frozen=True)
class Rung:
    width: int
    height: int
    bit_rate: int


def _rate(r: Rung, fps: tuple[int, int], frames: int = 1) -> dict[str, str]:
    # CBR with a buffer of a frame or two caps keyframe bursts.
    return {"b": str(r.bit_rate), "maxrate": str(r.bit_rate),
            "bufsize": str(r.bit_rate * frames * fps[1] // fps[0])}


_NVENC_LOW_DELAY = {"preset": "p1", "tune": "ull", "rc": "cbr", "bf": "0", "delay": "0",
                    "zerolatency": "1", "forced-idr": "1", "rc-lookahead": "0", "g": "100000"}


def _nvenc(r: Rung, fps: tuple[int, int], codec: str) -> Branch:
    # h264_nvenc overruns a one-frame buffer, by up to a fifth at 1080p60; two frames hold.
    return Branch(
        filter=f"scale_cuda=w={r.width}:h={r.height}",
        encoder=f"{codec}_nvenc",
        options={**_NVENC_LOW_DELAY, **_rate(r, fps, 2 if codec == "h264" else 1)},
        strip_filler=True)


def _cpu(r: Rung, fps: tuple[int, int], codec: str) -> Branch:
    if codec == "av1":
        # libaom, not SVT-AV1: SVT-AV1 1.7 ignores forced keyframes.
        enc, opts = "libaom-av1", {"usage": "realtime", "cpu-used": "8", "lag-in-frames": "0",
                                   "row-mt": "1"}
    elif have_encoder("libopenh264"):
        enc, opts = "libopenh264", {}
    else:
        enc, opts = "libx264", {"preset": "veryfast", "tune": "zerolatency", "forced-idr": "1",
                                "x264-params": "scenecut=0"}
    return Branch(
        filter=f"scale=w={r.width}:h={r.height}:flags=bilinear,format=yuv420p",
        encoder=enc,
        options={"g": "100000", "bf": "0", **opts, **_rate(r, fps)},
        strip_filler=codec == "av1")


def _videotoolbox(r: Rung, fps: tuple[int, int], codec: str) -> Branch:
    if codec != "h264":
        raise ValueError(f"videotoolbox profile: no {codec} encoder")
    return Branch(
        filter=f"scale_vt=w={r.width}:h={r.height}",
        encoder="h264_videotoolbox",
        options={"flags": "low_delay", "realtime": "1", "allow_sw": "0", "prio_speed": "1",
                 "bf": "0", "g": "100000", **_rate(r, fps)},
        strip_filler=True)


CODECS = ("h264", "av1")

PROFILES: dict[str, tuple[str | None, Callable[[Rung, tuple[int, int], str], Branch]]] = {
    "nvenc": ("cuda", _nvenc),
    "videotoolbox": ("videotoolbox", _videotoolbox),
    "cpu": (None, _cpu),
}


def ladder(rungs: Iterable[Rung], *, profile: str = "nvenc", codec: str = "h264",
           fps: tuple[int, int] | None = None, source: Source | None = None,
           **graph_kw) -> Graph:
    """A graph with one branch per rung, in order, encoding `codec` (h264 or av1). A `source`
    sets the decoder, its configuration and the frame rate (default 60) from its video."""
    if codec not in CODECS:
        raise ValueError(f"codec {codec!r}: one of {', '.join(CODECS)}")
    if source is not None and source.video is not None:
        graph_kw.setdefault("decoder", source.video.codec)
        graph_kw.setdefault("extradata", source.video.extradata)
        fps = fps or source.video.fps
    fps = fps or (60, 1)
    hwaccel, make = PROFILES[profile]
    return Graph([make(r, fps, codec) for r in rungs], hwaccel=hwaccel, fps=fps, **graph_kw)
