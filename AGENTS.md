# AGENTS.md

Contributor guide for `cymedia`.

## Layout

| Path | What |
|---|---|
| `src/cymedia/engine.c`, `engine.h` | the graph engine in C: decode thread, per-branch encode threads, queues, drop policy |
| `src/cymedia/cymedia_sink.h` | the output ABI; changing it means bumping `CYMEDIA_SINK_ABI` |
| `src/cymedia/qsink.c` | the queue sink behind `Graph.read()`, and a counting sink for tests |
| `src/cymedia/_core.pyx` | the Python binding |
| `src/cymedia/ladder.py` | ABR ladder profiles (`nvenc`, `videotoolbox`, `cpu`) |
| `tests/` | pytest; `test_graph_gpu.py` skips without CUDA, `test_graph_videotoolbox.py` without VideoToolbox |
| `bench/ladder_delay.py` | live added-delay bench |
| `scripts/build-ffmpeg.sh` | builds a pinned FFmpeg with what the profiles need |
| `.github/workflows/release.yml` | build, test and publish a release (see Release) |

## Rules

- **No transport or protocol knowledge.** cymedia never learns about MoQT, QUIC, sessions or
  peers. Group and object numbers are opaque values copied from input to output.
- **Never block a live producer.** In realtime mode queues drop at group granularity: input
  resumes at a group start, a frame of an older group is dropped, and a lagging branch abandons
  the rest of a group. Input queues are bounded in time (`input_max_ms`) as well as frames.
- **The GIL stays released** in every engine thread; Python only touches control calls and
  `read()`.
- **Tests:** anything with per-branch state gets a test with at least two branches. A test that
  expects drops asserts that drops happened.
- **Licensing:** a distributed build links an LGPL-only FFmpeg (no `--enable-gpl`, no
  `--enable-nonfree`; `scale_cuda` built with `--enable-cuda-llvm`) and publishes that FFmpeg's
  source and configure line, per [FFmpeg's checklist](https://ffmpeg.org/legal.html). Distribution
  packages often ship GPL builds: fine for development, not for redistributed wheels.
- **Comments** say what or why in few words, never history.

## Run

    .venv/bin/pip install --no-build-isolation -e . && .venv/bin/python -m pytest -q

GPU work runs on a host with an NVIDIA GPU or on Apple silicon. GeForce cards cap NVENC at a fixed number of
concurrent sessions; a six-rung ladder uses six.

## Release

The version is static in `pyproject.toml`, PEP 440: `0.1.0a1`, `0.1.0b1`, `0.1.0rc1`, `0.1.0`.

1. Land the version and its `## <version>` CHANGELOG section on main.
2. Dispatch `release.yml` on main with that version and `dry_run` on. It builds the sdist, checks
   it, installs it in a clean venv and runs the tests against it on Linux and macOS.
3. Dispatch again with `dry_run` off. It publishes to PyPI by trusted publishing (environment
   `pypi`), then tags `v<version>` with a GitHub release, a prerelease for a, b and rc.

A version cannot be uploaded twice: a broken release is fixed by the next version.

