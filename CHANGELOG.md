# Changelog

## 0.1.0a1

First alpha.

- Graph engine: decode once, a filter chain and an encoder per branch, source group and object
  numbers carried to every output, a keyframe at each group start.
- Realtime drop policy at group granularity, with input and branch events; frames wait while a
  branch's encoder first opens.
- C sink ABI, a queue sink for Python, and native sinks passed as capsules.
- ABR ladder profiles for NVENC and software, and a live delay bench.
