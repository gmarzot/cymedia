import json
import shutil
import struct
import subprocess

import pytest


def split_access_units(data: bytes) -> list[bytes]:
    """Splits an Annex B stream at access unit delimiters (NAL type 9)."""
    starts, i = [], 0
    while (k := data.find(b"\x00\x00\x01", i)) >= 0 and k + 3 < len(data):
        if data[k + 3] & 0x1F == 9:
            starts.append(k - 1 if k > 0 and data[k - 1] == 0 else k)
        i = k + 3
    return [data[a:b] for a, b in zip(starts, starts[1:] + [len(data)])]


def nal_types(frame: bytes) -> list[int]:
    out, i = [], 0
    while (k := frame.find(b"\x00\x00\x01", i)) >= 0 and k + 3 < len(frame):
        out.append(frame[k + 3] & 0x1F)
        i = k + 3
    return out


def obu_types(tu: bytes) -> list[int]:
    """The OBU types of an AV1 temporal unit in low-overhead form."""
    out, i = [], 0
    while i < len(tu):
        h = tu[i]
        i += 1 + ((h >> 2) & 1)
        size = 0
        for shift in range(0, 64, 7):
            c = tu[i]
            i += 1
            size |= (c & 0x7F) << shift
            if not c & 0x80:
                break
        out.append((h >> 3) & 15)
        i += size
    return out


def write_ivf(path, frames: list[bytes], width: int, height: int, fps: int) -> None:
    """AV1 temporal units in an IVF file, for ffprobe."""
    head = b"DKIF" + struct.pack("<HH4sHHIIII", 0, 32, b"AV01", width, height, fps, 1,
                                 len(frames), 0)
    body = b"".join(struct.pack("<IQ", len(f), n) + f for n, f in enumerate(frames))
    path.write_bytes(head + body)


def make_source(tmp_path, width=640, height=360, frames=90, gop=30, rate=30,
                noise=0) -> list[bytes]:
    """A testsrc2 H.264 stream with an IDR every `gop` frames, as access units;
    `noise` adds temporal noise of that strength, to load rate control."""
    if not shutil.which("ffmpeg"):
        pytest.skip("needs the ffmpeg command")
    out = tmp_path / f"src_{width}x{height}_{frames}_{gop}_{noise}.h264"
    vf = f",noise=alls={noise}:allf=t" if noise else ""
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
           "-i", f"testsrc2=size={width}x{height}:rate={rate}{vf}", "-frames:v", str(frames),
           "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-bf", "0",
           "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
           "-bsf:v", "h264_metadata=aud=insert", "-f", "h264", str(out)]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode:
        pytest.skip(f"ffmpeg cannot make an H.264 source: {r.stderr.decode()[-200:]}")
    aus = split_access_units(out.read_bytes())
    assert len(aus) == frames
    return aus


def probe_frames(path) -> list[dict]:
    """ffprobe's frames of a raw H.264 file: key_frame, width, height."""
    if not shutil.which("ffprobe"):
        pytest.skip("needs the ffprobe command")
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_frames",
                        "-show_entries", "frame=key_frame,width,height", "-of", "json", str(path)],
                       capture_output=True, check=True)
    return json.loads(r.stdout)["frames"]


def run_graph(graph, aus, group_size, fps=30, timeout=60):
    """Pushes every access unit with source ids, flushes, and returns outputs by branch."""
    step = 1_000_000 // fps
    outs = []
    for i, au in enumerate(aus):
        graph.push(au, i // group_size, i % group_size, i * step, i % group_size == 0)
        outs += graph.read(timeout=0)
    assert graph.flush(timeout=timeout)
    outs += graph.read(timeout=0)
    by_branch = {}
    for o in outs:
        by_branch.setdefault(o.branch, []).append(o)
    return by_branch
