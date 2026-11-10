# cymedia

Low-latency, group-aligned media processing graphs on FFmpeg's libraries, for Python.

A graph decodes a source once and feeds any number of branches. Each branch is a libavfilter
chain ending in an encoder, and its output goes to a sink. Source group numbers, object numbers
and timestamps pass through unchanged, and every branch starts a keyframe at each source group
start, so equally numbered groups line up across all outputs. The first graph is an ABR ladder.

**Status: alpha.** H.264 and AV1 ladders on the NVIDIA path (NVDEC, `scale_cuda`, NVENC) and in
software, and H.264 ladders on Apple's VideoToolbox, on Linux and macOS with Python 3.12 and 3.13. The API can change before 1.0.

## Install

cymedia builds against the system's FFmpeg, 6.1 or later. It needs FFmpeg's development files,
`pkg-config` and a C compiler:

    sudo apt-get install pkg-config libavcodec-dev libavfilter-dev libavutil-dev   # Ubuntu 24.04
    brew install ffmpeg pkg-config                                                 # macOS
    pip install cymedia

Prebuilt wheels with an LGPL FFmpeg are planned.

Packaged FFmpeg builds differ in what they include; Homebrew's has no libaom, for example.
[`scripts/build-ffmpeg.sh`](https://github.com/gmarzot/cymedia/blob/main/scripts/build-ffmpeg.sh)
builds one with what the profiles need, in a directory of its own:

    scripts/build-ffmpeg.sh
    source ~/.local/cymedia-ffmpeg/env.sh
    pip install --no-cache-dir --no-binary cymedia cymedia

That is an LGPL FFmpeg with libaom, plus NVENC and `scale_cuda` on Linux or VideoToolbox on macOS.
It has no software H.264 encoder: `--gpl` adds libx264 and makes the build GPL.

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
  the frame was dropped: input full in realtime mode, or a frame of a group older than one already
  pushed, which would stall the decoder until the next keyframe. A group start more than one group
  back is taken as a restarted source.
- **Output** is one `Output` per encoded frame, carrying its source frame's group, object and
  timestamp: Annex B for H.264, a temporal unit of OBUs for AV1. `g.fileno()` is readable while outputs are queued, for an event loop:
  `loop.add_reader(g.fileno(), lambda: handle(g.read(0)))`.
- **`flush(timeout)`** ends input and waits for every branch's last frame; `stats()` and
  `delays(branch)` report counters and recent push-to-output delays.

### Profiles

| Profile | Decode | Scale | `codec="h264"` | `codec="av1"` |
|---|---|---|---|---|
| `nvenc` | NVDEC | `scale_cuda` | `h264_nvenc` | `av1_nvenc` |
| `videotoolbox` | VideoToolbox | `scale_vt` | `h264_videotoolbox` | none |
| `cpu` | software | `scale` | libopenh264, else libx264 (veryfast, zerolatency) | libaom (realtime, cpu-used 8) |

The NVENC encoders run p1, ultra low latency, CBR with a one-frame buffer and no B-frames; H.264
filler NAL units and AV1 padding OBUs are stripped. Every profile forces a keyframe at each group
start. VideoToolbox runs in its low-latency mode with no B-frames. SVT-AV1 is not used: version
1.7 ignores forced keyframes.

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
held.

An Apple M4, a 1080p60 source to four VideoToolbox rungs (720p to 180p):
p50 12 ms on the first rung and 75–80 ms on the others, in realtime with no drops. It cannot keep
up with a 2160p60 source to six rungs.

[`bench/ladder_delay.py`](https://github.com/gmarzot/cymedia/blob/main/bench/ladder_delay.py) measures it on your hardware.

### Against the alternatives

The same job from an asyncio loop on the RTX 4070, 2160p60 H.264 to six NVENC rungs, medians of
three 120 s runs. Delay runs from handing over a frame to Python holding its encoded bytes.

| Path | p50 by rung | p99, slowest rung | CPU cores | 1 ms timer late, p99 | Frames out, worst run |
|---|---|---|---|---|---|
| cymedia | 8.4–9.2 ms | 14.6 ms | 0.39 | 0.98 ms | 7200 / 7200 |
| FFmpeg 6.1 CLI over pipes | 7.7–13.4 ms | 1443 ms | 0.23 | 0.20 ms | 6648 / 7200 |
| PyAV 19 threads, swscale | 14.2–19.7 ms | 23.6 ms | 2.04 | 1.17 ms | 7200 / 7200 |

With nothing running the timer is 0.44 ms late. The CLI encodes rungs one after another, so delay
climbs down the ladder; in every run it stalled for up to 1.5 s and lost groups, with the GPU
sampled in a low power state, and one run sat at a 55–62 ms p50. FFmpeg's newer threaded CLI was
not measured. PyAV's wheels have no GPU scaler, so frames come back to system memory.
[`bench/compare_paths.py`](https://github.com/gmarzot/cymedia/blob/main/bench/compare_paths.py)
repeats this. That host's clock ran about 4% slow, so the source was really about 57.5 fps and every
path's delays read about 4% low.

## Used by

[aiomoqt](https://github.com/gmarzot/aiomoqt)'s `abr_ingest` sits in front of a MoQ relay and
publishes a broadcast plus a cymedia ladder under one catalog.

## Develop

    python -m venv .venv && .venv/bin/pip install "Cython>=3.0.10" "setuptools>=77" pytest
    .venv/bin/pip install --no-build-isolation -e .
    .venv/bin/python -m pytest -q

GPU tests skip unless CUDA, `h264_nvenc` and `scale_cuda` are available. The delay bench:

    .venv/bin/python bench/ladder_delay.py clip.mkv --profile nvenc --seconds 90
    .venv/bin/python bench/compare_paths.py clip.mkv  # NVENC, against the CLI and PyAV

Contributor rules are in [AGENTS.md](https://github.com/gmarzot/cymedia/blob/main/AGENTS.md).

## License

MIT. This software uses code of [FFmpeg](https://ffmpeg.org) licensed under the
[LGPLv2.1](https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html) and its source can be
downloaded [here](https://ffmpeg.org/download.html). cymedia ships no FFmpeg: it links the
system's, and `cymedia.ffmpeg_info()` reports that build's license. Redistributing FFmpeg with
cymedia is subject to [FFmpeg's terms](https://ffmpeg.org/legal.html).
