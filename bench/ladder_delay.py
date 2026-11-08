"""Added delay of a cymedia ladder fed in real time from a clip.

Each frame's delay runs from push to the branch's output, so it covers decode, filter, queueing
and encode. Run past 60 s: some GPUs only drop to a low-power state after that.

    python bench/ladder_delay.py CLIP [--profile nvenc] [--seconds 90] [--rate 1.0] [--rungs 6]
"""
import argparse
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import cymedia
from cymedia import Rung, ladder

LADDER = [Rung(2560, 1440, 13_500_000), Rung(1920, 1080, 6_750_000), Rung(1280, 720, 3_750_000),
          Rung(960, 540, 2_250_000), Rung(640, 360, 900_000), Rung(320, 180, 300_000)]


def access_units(clip: str) -> list[bytes]:
    out = Path(tempfile.mkdtemp()) / "src.h264"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", clip, "-map", "0:v:0", "-c", "copy",
                    "-bsf:v", "h264_mp4toannexb,h264_metadata=aud=insert", "-f", "h264", str(out)],
                   check=True)
    data, starts, i = out.read_bytes(), [], 0
    while (k := data.find(b"\x00\x00\x01", i)) >= 0 and k + 3 < len(data):
        if data[k + 3] & 0x1F == 9:
            starts.append(k - 1 if k > 0 and data[k - 1] == 0 else k)
        i = k + 3
    return [data[a:b] for a, b in zip(starts, starts[1:] + [len(data)])]


def is_idr(au: bytes) -> bool:
    i = 0
    while (k := au.find(b"\x00\x00\x01", i)) >= 0 and k + 3 < len(au):
        if au[k + 3] & 0x1F == 5:
            return True
        i = k + 3
    return False


def gpu_sampler(stop: threading.Event, samples: list[str]) -> None:
    smi = shutil.which("nvidia-smi") or "/usr/lib/wsl/lib/nvidia-smi"
    while not stop.wait(10):
        r = subprocess.run([smi, "--query-gpu=pstate,clocks.mem,utilization.encoder",
                            "--format=csv,noheader"], capture_output=True, text=True)
        if r.returncode == 0:
            samples.append(r.stdout.strip())


def ms(ns: list[int], q: float) -> float:
    return statistics.quantiles(ns, n=100)[int(q) - 1] / 1e6 if len(ns) > 1 else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("clip")
    ap.add_argument("--profile", default="nvenc", choices=sorted(cymedia.PROFILES))
    ap.add_argument("--seconds", type=float, default=90)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--rate", type=float, default=1.0, help="push rate as a fraction of --fps")
    ap.add_argument("--rungs", type=int, default=len(LADDER))
    args = ap.parse_args()
    cymedia.set_ffmpeg_log_level(24)

    aus = access_units(args.clip)
    idr = [is_idr(a) for a in aus]
    if not idr[0]:
        print("the clip must start with an IDR", file=sys.stderr)
        return 2
    graph = ladder(LADDER[:args.rungs], profile=args.profile, fps=(args.fps, 1), realtime=True)
    counts = [dict(out=0, key_at_start=0, key_elsewhere=0, start_without_key=0)
              for _ in range(args.rungs)]
    stop = threading.Event()

    def drain() -> None:
        while True:
            outs = graph.read(timeout=0.2)
            if not outs and stop.is_set():
                return
            for o in outs:
                c = counts[o.branch]
                c["out"] += 1
                if o.key:
                    c["key_at_start" if o.group_start else "key_elsewhere"] += 1
                elif o.group_start:
                    c["start_without_key"] += 1

    gpu: list[str] = []
    readers = [threading.Thread(target=drain),
               threading.Thread(target=gpu_sampler, args=(stop, gpu))]
    for t in readers:
        t.start()
    frames = int(args.seconds * args.fps)
    period = 1 / (args.fps * args.rate)
    group, obj, t0, late = -1, 0, time.monotonic(), 0
    for n in range(frames):
        k = n % len(aus)
        if idr[k]:
            group, obj = group + 1, 0
        wait = t0 + n * period - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        elif wait < -period:
            late += 1
        graph.push(aus[k], group, obj, n * 1_000_000 // args.fps, idr[k])
        obj += 1
    graph.flush(timeout=30)
    stop.set()
    for t in readers:
        t.join()
    st = graph.stats()
    print(f"{args.profile}: {frames} frames at {args.fps * args.rate:g} fps, {args.seconds:g} s, "
          f"input dropped {st['packets_dropped']}, decode errors {st['decode_errors']}, "
          f"pushes late by over a frame {late}")
    print(f"{'rung':>10} {'out':>6} {'drop':>5} {'aband':>5} {'p50 ms':>7} {'p99 ms':>7} "
          f"{'max ms':>7}  keys at group starts / elsewhere / starts without a key")
    for b, r in enumerate(LADDER[:args.rungs]):
        d = graph.delays(b)
        bs, c = st["branches"][b], counts[b]
        print(f"{r.width:>5}x{r.height:<4} {c['out']:>6} {bs['frames_dropped']:>5} "
              f"{bs['groups_abandoned']:>5} {ms(d, 50):>7.2f} {ms(d, 99):>7.2f} "
              f"{max(d) / 1e6 if d else float('nan'):>7.2f}  "
              f"{c['key_at_start']} / {c['key_elsewhere']} / {c['start_without_key']}")
    if gpu:
        print("gpu (pstate, memory clock, encoder use):", "; ".join(sorted(set(gpu))))
    graph.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
