# cymedia

Low-latency, group-aligned media processing graphs on FFmpeg's libraries, for Python.

A graph decodes a source once and feeds any number of branches. Each branch is a libavfilter
chain ending in an encoder, and its output goes to a sink. Source group numbers, object numbers
and timestamps pass through unchanged, and every branch starts a keyframe at each source group
start, so equally numbered groups line up across all outputs. The first graph is an ABR ladder.

**Status: alpha.** H.264 and AV1 ladders on the NVIDIA path (NVDEC, `scale_cuda`, NVENC) and in
software, on Linux and macOS with Python 3.12 and 3.13. The API can change before 1.0.

## Install

cymedia builds against the system's FFmpeg, 6.1 or later. It needs FFmpeg's development files,
`pkg-config` and a C compiler:

    sudo apt-get install pkg-config libavcodec-dev libavfilter-dev libavutil-dev   # Ubuntu 24.04
    brew install ffmpeg pkg-config                                                 # macOS
    pip install cymedia

Prebuilt wheels with an LGPL FFmpeg are planned.

## Example

```python
from cymedia import Rung, ladder

rungs = [Rung(1280, 720, 3_750_000), Rung(640, 360, 900_000)]
with ladder(rungs, profile="nvenc", codec="h264", fps=(60, 1)) as g:
    g.push(frame, group, obj, pts_us, key)  # one H.264 access unit
    for out in g.read(timeout=0.1):
        ...  # out.branch, out.group, out.object, out.pts_us, out.key, out.group_start, out.data
```

- **Input** is one H.264 access unit per `push()`: Annex B, or 4-byte length prefixes with the
  avcC passed as `extradata=`. `key` marks a group's first frame. `push()` returns False when
  the frame was dropped.
- **Output** is one `Output` per encoded frame, carrying its source frame's group, object and
  timestamp: Annex B for H.264, a temporal unit of OBUs for AV1. `g.fileno()` is readable while outputs are queued, for an event loop:
  `loop.add_reader(g.fileno(), lambda: handle(g.read(0)))`.
- **`flush(timeout)`** ends input and waits for every branch's last frame; `stats()` and
  `delays(branch)` report counters and recent push-to-output delays.

### Profiles

| Profile | Decode | Scale | `codec="h264"` | `codec="av1"` |
|---|---|---|---|---|
| `nvenc` | NVDEC | `scale_cuda` | `h264_nvenc` | `av1_nvenc` |
| `cpu` | software | `scale` | libopenh264, else libx264 (veryfast, zerolatency) | libaom (realtime, cpu-used 8) |

The NVENC encoders run p1, ultra low latency, CBR with a one-frame buffer and no B-frames; H.264
filler NAL units and AV1 padding OBUs are stripped. Every profile forces a keyframe at each group
start. SVT-AV1 is not used: version 1.7 ignores forced keyframes.

Any other graph is a list of branches:

```python
from cymedia import Branch, Graph

g = Graph([Branch("scale=640:360,format=yuv420p", "libx264", {"preset": "veryfast"})],
          decoder="h264", fps=(30, 1))
```

## How it runs

- One decode thread decodes each source frame once, then runs every branch's filter chain.
- Each branch encodes on its own thread. Python's GIL is released throughout.
- In realtime mode (the default) a full queue drops instead of blocking. Input resumes at the next
  group start; a branch that falls behind abandons the rest of that group, so a group is cut
  short and never has a hole in it. While a branch's encoder first opens, frames wait rather
  than drop. With `realtime=False`, queues block instead.
- Outputs go to a sink with a C ABI ([`cymedia_sink.h`](https://github.com/gmarzot/cymedia/blob/main/src/cymedia/cymedia_sink.h)). The
  default sink queues them for `Graph.read()`; a native consumer passes its own sink as a
  `cymedia.sink` capsule and keeps frames out of Python entirely.

## Measured delay

An RTX 4070, a 2160p60 H.264 source to six NVENC rungs (1440p to 180p), from `push()` to the
encoded output, the 4K decode included (about 4.4 ms of it):

| Run | GPU power state | p50 | p99 |
|---|---|---|---|
| 60 fps | P2, then P3 | 8.5–9.3 ms | 14.6–15.3 ms |
| 60 fps, clocks held up | P2 | 7.4–8.0 ms | 15.5–17.1 ms |
| 30 fps | P5 | 14.7–16.1 ms | 29.7–30.7 ms |
| 30 fps, clocks held up | P2/P3 | 8.5–9.0 ms | 16.0–16.9 ms |

Under a light load a GeForce card drops to a low power state and the delay doubles; no frames are
held. [`bench/ladder_delay.py`](https://github.com/gmarzot/cymedia/blob/main/bench/ladder_delay.py) measures it on your hardware.

## Used by

[aiomoqt](https://github.com/gmarzot/aiomoqt)'s `abr_ingest` sits in front of a MoQ relay and
publishes a broadcast plus a cymedia ladder under one catalog.

## Develop

    python -m venv .venv && .venv/bin/pip install "Cython>=3.0.10" "setuptools>=77" pytest
    .venv/bin/pip install --no-build-isolation -e .
    .venv/bin/python -m pytest -q

GPU tests skip unless CUDA, `h264_nvenc` and `scale_cuda` are available. The delay bench:

    .venv/bin/python bench/ladder_delay.py clip.mkv --profile nvenc --seconds 90

Contributor rules are in [AGENTS.md](https://github.com/gmarzot/cymedia/blob/main/AGENTS.md).

## License

MIT. cymedia links FFmpeg dynamically. A build for distribution must link an LGPL-only FFmpeg
(no `--enable-gpl`, no `--enable-nonfree`; `scale_cuda` built with `--enable-cuda-llvm`) and
publish that FFmpeg's source and configure line. Distribution packages often ship GPL builds,
which are fine for development but not for redistributed wheels. `cymedia.ffmpeg_info()` reports
the license of the FFmpeg in use.
