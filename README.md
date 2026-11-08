# cymedia

Low-latency, group-aligned media processing graphs on FFmpeg's libraries, for Python.

A graph decodes a source once, then feeds any number of branches. Each branch is a libavfilter
chain ending in an encoder, and each branch's output goes to a sink. Source group numbers, object
numbers and timestamps pass through unchanged, and every branch starts a keyframe at each source
group start, so equally numbered groups line up across all outputs. The first graph is an ABR
ladder.

Status: early development. NVIDIA (NVDEC, `scale_cuda`, NVENC) and a software path.

```python
from cymedia import Rung, ladder

with ladder([Rung(1280, 720, 3_750_000), Rung(640, 360, 900_000)], profile="nvenc") as g:
    g.push(frame, group, obj, pts_us, key)  # one encoded source frame (H.264, Annex B)
    for out in g.read(timeout=0.1):         # out.branch, out.group, out.object, out.key, out.data
        ...
```

## How it runs

- One decode thread decodes each source frame once, then runs every branch's filter chain.
- Each branch encodes on its own thread. Python's GIL is released throughout.
- In realtime mode a full queue drops instead of blocking. Input resumes at the next group start;
  a branch that falls behind abandons the rest of that group (`CYMEDIA_EV_GROUP_ABANDONED`), so a
  group is cut short and never has a hole in it. While a branch's encoder first opens, frames wait
  rather than drop; the input queue takes up the slack.
- Outputs go to a sink with a C ABI (`src/cymedia/cymedia_sink.h`). The default sink queues them
  for `Graph.read()`; a native consumer can pass its own sink as a `cymedia.sink` capsule and keep
  frames out of Python entirely.

## Build

Needs FFmpeg's development files (libavcodec, libavfilter, libavutil) and `pkg-config`.

    python -m venv .venv && .venv/bin/pip install "Cython>=3.0.10" "setuptools>=77" pytest
    .venv/bin/pip install --no-build-isolation -e .
    .venv/bin/python -m pytest -q

GPU tests skip unless CUDA, `h264_nvenc` and `scale_cuda` are available. The delay bench:

    .venv/bin/python bench/ladder_delay.py clip.mkv --profile nvenc --seconds 90

## License

MIT. cymedia links FFmpeg dynamically. A build for distribution must link an LGPL-only FFmpeg
(no `--enable-gpl`, no `--enable-nonfree`; `scale_cuda` built with `--enable-cuda-llvm`) and
publish that FFmpeg's source and configure line. Distribution packages often ship GPL builds,
which are fine for development but not for redistributed wheels. `cymedia.ffmpeg_info()` reports
the license of the FFmpeg in use.
