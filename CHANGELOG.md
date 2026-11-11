# Changelog

## Unreleased

- In realtime mode, a packet that has waited `input_max_ms` to be decoded (default 500, 0 to turn
  off) is dropped with the rest of its group, once every encoder has opened. Overload had added
  up to the 240-frame input queue's worth of delay.

## 0.1.0

- `push()` drops a frame of a group older than one already pushed, with an input-dropped
  event, instead of decoding it: a late group tail stalled the decoder until the next keyframe.
- Python 3.14: tested in CI and in the release smoke test.
- `bench/compare_paths.py`: the same NVENC ladder through cymedia, the FFmpeg CLI and PyAV.

## 0.1.0a3

- `videotoolbox` ladder profile: H.264 on Apple's VideoToolbox.
- `scripts/build-ffmpeg.sh` builds a pinned FFmpeg with what the profiles need, on Linux and macOS.
- Hardware profiles work on FFmpeg 9, which needs the hardware frames context before the filter
  source initializes.
- A build links the FFmpeg `pkg-config` names, even when Python's library directory holds another.

## 0.1.0a2

- AV1 ladders: `ladder(..., codec="av1")` encodes with `av1_nvenc`, or libaom in realtime mode
  on the `cpu` profile, a keyframe at each group start; padding OBUs are stripped like H.264
  filler. `CODECS` lists the codecs, and `PROFILES` makers take the codec.
- An encoder that fails to open is retried only when the frame size or format changes, not on
  every frame.

## 0.1.0a1

First alpha.

- Graph engine: decode once, a filter chain and an encoder per branch, source group and object
  numbers carried to every output, a keyframe at each group start.
- Realtime drop policy at group granularity, with input and branch events; frames wait while a
  branch's encoder first opens.
- C sink ABI, a queue sink for Python, and native sinks passed as capsules.
- ABR ladder profiles for NVENC and software, and a live delay bench.
