"""One NVENC ladder from Python three ways: cymedia, the FFmpeg CLI over pipes, a PyAV loop.

Each run is its own process. The same H.264 access units are handed over at the source's rate
from an asyncio loop, as an ingester would. Delay runs from handing a frame over to Python
holding that frame's encoded bytes. The first 10 s and the last second of each run are left out.

Loop lateness is how far a 1 ms asyncio timer overshoots while the ladder runs; the "none" path
gives its floor. PyAV's wheels have no GPU scaler, so that path downloads decoded frames and
scales with swscale. The pyav path needs PyAV installed; leave it out with --paths otherwise.

    python bench/compare_paths.py CLIP [--seconds 120] [--repeat 3] [--out results.jsonl]
"""
import argparse
import asyncio
import json
import os
import queue
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
from fractions import Fraction

import cymedia
from ladder_delay import LADDER, access_units, gpu_sampler, is_idr, ms

PATHS = ("cymedia", "ffmpeg-cli", "pyav", "none")
FPS = 60
WARMUP_S = 10
QUEUE_FRAMES = 60
CLK_TCK = os.sysconf("SC_CLK_TCK")


def cpu_seconds(pid: int) -> float:
    with open(f"/proc/{pid}/stat") as f:
        fields = f.read().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / CLK_TCK


def nvenc_branch(rung):
    return cymedia.PROFILES["nvenc"][1](rung, (FPS, 1), "h264")


class Recorder:
    def __init__(self, rungs: int, frames: int) -> None:
        self.pushed = [0] * frames
        self.delays: list[list[int]] = [[] for _ in range(rungs)]
        self.out = [0] * rungs
        self.keys = [0] * rungs
        self.window = (0, 0)

    def done(self, rung: int, n: int, key: bool, now: int | None = None) -> None:
        now = now or time.monotonic_ns()
        self.out[rung] += 1
        self.keys[rung] += key
        if self.window[0] <= self.pushed[n] < self.window[1]:
            self.delays[rung].append(now - self.pushed[n])


class Nothing:
    pids: tuple[int, ...] = ()

    def __init__(self, rec, rungs, is_key, size) -> None:
        pass

    def push(self, n, au, group, obj, pts_us, key) -> None:
        pass

    async def finish(self) -> list[int]:
        return []


class Cymedia(Nothing):
    def __init__(self, rec, rungs, is_key, size) -> None:
        self.rec = rec
        self.graph = cymedia.ladder(rungs, profile="nvenc", codec="h264", fps=(FPS, 1),
                                    realtime=True)
        asyncio.get_running_loop().add_reader(self.graph.fileno(), self.read)

    def read(self) -> None:
        for o in self.graph.read(0):
            self.rec.done(o.branch, (o.pts_us * FPS + 500_000) // 1_000_000, o.key)

    def push(self, n, au, group, obj, pts_us, key) -> None:
        self.graph.push(au, group, obj, pts_us, key)

    async def finish(self) -> list[int]:
        await asyncio.to_thread(self.graph.flush, 30)
        asyncio.get_running_loop().remove_reader(self.graph.fileno())
        self.read()
        stats = self.graph.stats()
        self.graph.close()
        return [b["frames_dropped"] + stats["packets_dropped"] for b in stats["branches"]]


class FfmpegCli(Nothing):
    """IVF in and FLV out carry frame boundaries and timestamps, so nothing waits for the next
    frame's start code and each output maps to its source frame."""

    def __init__(self, rec, rungs, is_key, size) -> None:
        self.rec = rec
        cmd = ["ffmpeg", "-v", "error", "-nostats", "-flags", "low_delay", "-copyts",
               "-probesize", "32", "-analyzeduration", "0", "-hwaccel", "cuda",
               "-hwaccel_output_format", "cuda", "-threads", "1", "-f", "ivf", "-i", "pipe:0"]
        reads, writes = [], []
        for r in rungs:
            b = nvenc_branch(r)
            rd, wr = os.pipe()
            reads.append(rd)
            writes.append(wr)
            cmd += ["-map", "0:v:0", "-vf", b.filter, "-c:v", b.encoder, "-fps_mode", "passthrough",
                    "-force_key_frames", "source", "-bsf:v", "filter_units=remove_types=12"]
            for k, v in b.options.items():
                cmd += [f"-{k}", v]
            cmd += ["-flush_packets", "1", "-flvflags", "no_metadata+no_duration_filesize",
                    "-f", "flv", f"pipe:{wr}"]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, pass_fds=writes)
        for wr in writes:
            os.close(wr)
        self.pids = (self.proc.pid,)
        self.input: queue.Queue = queue.Queue(QUEUE_FRAMES)
        self.dropped = 0
        self.threads = [threading.Thread(target=self.writer, args=(size,))]
        self.threads += [threading.Thread(target=self.reader, args=(i, rd))
                         for i, rd in enumerate(reads)]
        for t in self.threads:
            t.start()

    def writer(self, size) -> None:
        w, h = size
        self.proc.stdin.write(struct.pack("<4sHH4sHHIIII", b"DKIF", 0, 32, b"H264", w, h,
                                          FPS, 1, 0, 0))
        while (item := self.input.get()) is not None:
            n, au = item
            self.proc.stdin.write(struct.pack("<IQ", len(au), n) + au)
            self.proc.stdin.flush()
        self.proc.stdin.close()

    def reader(self, rung: int, fd: int) -> None:
        buf, pos = bytearray(), 13  # past the FLV header
        while chunk := os.read(fd, 1 << 20):
            now = time.monotonic_ns()
            buf += chunk
            while len(buf) >= pos + 11:
                end = pos + 11 + int.from_bytes(buf[pos + 1:pos + 4], "big") + 4
                if len(buf) < end:
                    break
                if buf[pos] == 9 and buf[pos + 12] == 1:  # video tag holding a frame
                    ts_ms = int.from_bytes(buf[pos + 4:pos + 7], "big") | buf[pos + 7] << 24
                    self.rec.done(rung, (ts_ms * FPS + 500) // 1000, buf[pos + 11] >> 4 == 1, now)
                pos = end
            del buf[:pos]
            pos = 0
        os.close(fd)

    def push(self, n, au, group, obj, pts_us, key) -> None:
        try:
            self.input.put_nowait((n, au))
        except queue.Full:
            self.dropped += 1

    async def finish(self) -> list[int]:
        self.input.put(None)
        await asyncio.to_thread(self.proc.wait)
        for t in self.threads:
            await asyncio.to_thread(t.join)
        return [self.dropped] * len(self.rec.out)


class PyavLoop(Nothing):
    """One decode thread and one scale-and-encode thread per rung."""

    def __init__(self, rec, rungs, is_key, size) -> None:
        import av
        from av.codec.context import Flags
        from av.codec.hwaccel import HWAccel
        from av.video.frame import PictureType
        self.av, self.intra = av, PictureType.I
        self.rec = rec
        self.is_key = is_key
        self.decoder = av.CodecContext.create(
            "h264", "r", hwaccel=HWAccel("cuda", allow_software_fallback=False))
        self.decoder.thread_count = 1
        self.decoder.flags |= Flags.low_delay
        self.input: queue.Queue = queue.Queue(QUEUE_FRAMES)
        self.branches = [queue.Queue(QUEUE_FRAMES) for _ in rungs]
        self.dropped = [0] * len(rungs)
        self.threads = [threading.Thread(target=self.decode)]
        for i, r in enumerate(rungs):
            self.threads.append(threading.Thread(target=self.encode, args=(i, self.encoder(r))))
        for t in self.threads:
            t.start()

    def decode(self) -> None:
        while (item := self.input.get()) is not None:
            n, au = item
            pkt = self.av.Packet(au)
            pkt.pts = pkt.dts = n
            pkt.time_base = Fraction(1, FPS)
            for frame in self.decoder.decode(pkt):
                for i, q in enumerate(self.branches):
                    try:
                        q.put_nowait(frame)
                    except queue.Full:
                        self.dropped[i] += 1
        for q in self.branches:
            q.put(None)

    def encoder(self, rung):
        branch = nvenc_branch(rung)
        enc = self.av.CodecContext.create(branch.encoder, "w")
        enc.width, enc.height, enc.pix_fmt = rung.width, rung.height, "nv12"
        enc.framerate, enc.time_base = Fraction(FPS), Fraction(1, FPS)
        enc.options = dict(branch.options)
        enc.open()
        return enc

    def encode(self, i: int, enc) -> None:
        q = self.branches[i]
        while (frame := q.get()) is not None:
            scaled = frame.reformat(enc.width, enc.height, "nv12", interpolation="BILINEAR")
            scaled.pts, scaled.time_base = frame.pts, enc.time_base
            if self.is_key[frame.pts]:
                scaled.pict_type = self.intra
            for pkt in enc.encode(scaled):
                self.rec.done(i, pkt.pts, pkt.is_keyframe)
        for pkt in enc.encode(None):
            self.rec.done(i, pkt.pts, pkt.is_keyframe)

    def push(self, n, au, group, obj, pts_us, key) -> None:
        self.is_key[n] = key
        try:
            self.input.put_nowait((n, au))
        except queue.Full:
            self.dropped = [d + 1 for d in self.dropped]

    async def finish(self) -> list[int]:
        self.input.put(None)
        for t in self.threads:
            await asyncio.to_thread(t.join)
        return self.dropped


IMPLS = {"cymedia": Cymedia, "ffmpeg-cli": FfmpegCli, "pyav": PyavLoop, "none": Nothing}


def clip_size(clip: str) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0", clip],
                         check=True, capture_output=True, text=True).stdout
    w, h = out.strip().split(",")[:2]
    return int(w), int(h)


async def run_path(name: str, clip: str, seconds: float) -> dict:
    aus = access_units(clip)
    idr = [is_idr(a) for a in aus]
    frames = int(seconds * FPS)
    rec = Recorder(len(LADDER) if name != "none" else 0, frames)
    path = IMPLS[name](rec, LADDER, bytearray(frames), clip_size(clip))
    stop, gpu = threading.Event(), []
    sampler = threading.Thread(target=gpu_sampler, args=(stop, gpu))
    sampler.start()

    t0 = time.monotonic()
    rec.window = (time.monotonic_ns() + WARMUP_S * 1_000_000_000,
                  time.monotonic_ns() + int((seconds - 1) * 1e9))
    pids = (os.getpid(), *path.pids)
    lateness: list[int] = []

    async def probe() -> None:
        while True:
            before = time.monotonic_ns()
            await asyncio.sleep(0.001)
            after = time.monotonic_ns()
            if rec.window[0] <= before < rec.window[1]:
                lateness.append(after - before - 1_000_000)

    prober = asyncio.create_task(probe())
    group, obj, groups, late, cpu0, window0 = -1, 0, 0, 0, None, 0.0
    for n in range(frames):
        k = n % len(aus)
        if idr[k]:
            group, obj, groups = group + 1, 0, groups + 1
        wait = t0 + n / FPS - time.monotonic()
        if wait < -1 / FPS:
            late += 1
        await asyncio.sleep(max(wait, 0))
        if cpu0 is None and n >= WARMUP_S * FPS:
            cpu0, window0 = sum(map(cpu_seconds, pids)), time.monotonic()
        rec.pushed[n] = time.monotonic_ns()
        path.push(n, aus[k], group, obj, n * 1_000_000 // FPS, idr[k])
        obj += 1
    cpu = (sum(map(cpu_seconds, pids)) - cpu0) / (time.monotonic() - window0)
    prober.cancel()
    dropped = await path.finish()
    stop.set()
    await asyncio.to_thread(sampler.join)
    return {
        "path": name, "frames": frames, "groups": groups, "late_pushes": late, "cpu_cores": cpu,
        "gpu": sorted(set(gpu)),
        "loop": {"p50": ms(lateness, 50), "p99": ms(lateness, 99), "max": max(lateness) / 1e6},
        "rungs": [{"name": f"{r.width}x{r.height}", "out": rec.out[i], "keys": rec.keys[i],
                   "dropped": dropped[i], "p50": ms(rec.delays[i], 50),
                   "p99": ms(rec.delays[i], 99),
                   "max": max(rec.delays[i]) / 1e6} for i, r in enumerate(LADDER)]
                  if name != "none" else [],
    }


def cell(values: list[float], fmt: str = ".1f") -> str:
    if not values:
        return "-"
    mid = f"{statistics.median(values):{fmt}}"
    return mid if len(values) == 1 else f"{mid} ({min(values):{fmt}}–{max(values):{fmt}})"


def report(results: list[dict], paths: list[str]) -> None:
    print("| Path | Fastest rung p50 ms | Slowest rung p50 ms | Slowest rung p99 ms | Worst ms | "
          "CPU cores | Loop late p99 ms | Loop late max ms | Dropped | Frames out, lowest rung | "
          "Keyframes | GPU |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for p in paths:
        rs = [r for r in results if r["path"] == p]
        if not rs:
            continue
        ran = [r for r in rs if r["rungs"]]
        out = (f"{min(min(x['out'] for x in r['rungs']) for r in ran)} / {ran[0]['frames']}"
               if ran else "-")
        keys = (f"{min(min(x['keys'] for x in r['rungs']) for r in ran)} / {ran[0]['groups']}"
                if ran else "-")
        print(f"| {p} | {cell([min(x['p50'] for x in r['rungs']) for r in ran])} "
              f"| {cell([max(x['p50'] for x in r['rungs']) for r in ran])} "
              f"| {cell([max(x['p99'] for x in r['rungs']) for r in ran])} "
              f"| {cell([max(x['max'] for x in r['rungs']) for r in ran])} "
              f"| {cell([r['cpu_cores'] for r in rs], '.2f')} "
              f"| {cell([r['loop']['p99'] for r in rs], '.2f')} "
              f"| {cell([r['loop']['max'] for r in rs], '.1f')} "
              f"| {sum(sum(x['dropped'] for x in r['rungs']) for r in ran) if ran else '-'} "
              f"| {out} | {keys} | {'; '.join(sorted({g for r in rs for g in r['gpu']}))} |")
    print()
    print("| Path | Rung | p50 ms | p99 ms |")
    print("|---|---|---|---|")
    for p in paths:
        ran = [r for r in results if r["path"] == p and r["rungs"]]
        for i, name in enumerate(x["name"] for x in (ran[0]["rungs"] if ran else [])):
            print(f"| {p} | {name} | {cell([r['rungs'][i]['p50'] for r in ran])} "
                  f"| {cell([r['rungs'][i]['p99'] for r in ran])} |")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("clip")
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--paths", default=",".join(PATHS))
    ap.add_argument("--out", help="also write each run's result, one JSON object per line")
    ap.add_argument("--run", choices=PATHS, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.run:
        cymedia.set_ffmpeg_log_level(24)
        print(json.dumps(asyncio.run(run_path(args.run, args.clip, args.seconds))))
        return 0

    paths = args.paths.split(",")
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for rep in range(args.repeat):
            for name in paths[rep % len(paths):] + paths[:rep % len(paths)]:
                print(f"run {rep + 1}/{args.repeat}: {name}", file=sys.stderr, flush=True)
                r = subprocess.run([sys.executable, __file__, args.clip, "--run", name,
                                    "--seconds", str(args.seconds)], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, text=True,
                                   env={**os.environ, "TMPDIR": tmp})
                if r.returncode:
                    print(f"{name} failed", file=sys.stderr)
                    return 1
                results.append(json.loads(r.stdout.splitlines()[-1]))
                time.sleep(3)
    if args.out:
        with open(args.out, "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in results)
    report(results, paths)
    return 0


if __name__ == "__main__":
    sys.exit(main())
