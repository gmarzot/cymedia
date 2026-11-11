import functools
import http.server
import shutil
import signal
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from helpers import probe_frames

import cymedia
from cymedia import Rung, Source, ladder

STEP = 1_000_000 / 30

needs_cpu_h264 = pytest.mark.skipif(
    not (cymedia.have_encoder("libx264") or cymedia.have_encoder("libopenh264")),
    reason="needs a software H.264 encoder")


def make_clip(tmp_path, ext, seconds=2):
    """testsrc2 at 30 fps with an IDR every 30 frames, and a 48 kHz AAC tone."""
    if not shutil.which("ffmpeg"):
        pytest.skip("needs the ffmpeg command")
    out = tmp_path / f"clip_{seconds}.{ext}"
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", str(seconds),
           "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-bf", "0",
           "-g", "30", "-keyint_min", "30", "-sc_threshold", "0", "-c:a", "aac", "-b:a", "64k",
           str(out)]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode:
        pytest.skip(f"ffmpeg cannot make a clip: {r.stderr.decode()[-200:]}")
    return out


def drain(src, timeout=20, video=None):
    """Reads until the source ends, or until `video` video packets arrived."""
    out = []
    deadline = time.monotonic() + timeout
    while not src.ended and time.monotonic() < deadline:
        out += src.read(timeout=0.2)
        if video and sum(p.stream == "video" for p in out) >= video:
            break
    return out


def nal_units(lp: bytes) -> list[int]:
    """NAL unit types of a length-prefixed payload; fails unless the lengths add up."""
    types, i = [], 0
    while i < len(lp):
        n = int.from_bytes(lp[i:i + 4], "big")
        assert n > 0 and i + 4 + n <= len(lp)
        types.append(lp[i + 4] & 0x1F)
        i += 4 + n
    return types


def check_video(video, frames):
    assert [(p.group, p.object) for p in video] == [(i // 30, i % 30) for i in range(frames)]
    assert [p.key for p in video] == [i % 30 == 0 for i in range(frames)]
    for i, p in enumerate(video):
        assert abs(p.pts_us - video[0].pts_us - i * STEP) <= 1
        types = nal_units(p.data)
        assert 9 not in types and (5 in types) == p.key


def check_audio(audio):
    assert audio and all(p.key for p in audio)
    assert [p.group for p in audio] == list(range(len(audio)))


@pytest.mark.parametrize("ext", ["mp4", "ts"])
def test_a_file_source_numbers_groups_at_key_frames(tmp_path, ext):
    clip = make_clip(tmp_path, ext)
    with Source(str(clip)) as src:
        v, a = src.video, src.audio
        assert (v.codec, v.width, v.height, v.fps, v.length_prefixed) == ("h264", 320, 180,
                                                                           (30, 1), True)
        assert v.extradata[0] == 1  # avcC
        assert (a.codec, a.sample_rate, a.channels) == ("aac", 48000, 1)
        src.start(pace=False)
        pkts = drain(src)
        st = src.stats()
    assert src.ended and src.error is None
    check_video([p for p in pkts if p.stream == "video"], 60)
    check_audio([p for p in pkts if p.stream == "audio"])
    assert st["video_packets"] == 60 and st["before_key"] == 0


@needs_cpu_h264
@pytest.mark.parametrize("ext", ["mp4", "ts"])
def test_a_source_feeds_a_ladder_natively(tmp_path, ext):
    clip = make_clip(tmp_path, ext)
    with Source(str(clip)) as src, \
            ladder([Rung(160, 90, 150_000)], profile="cpu", source=src, realtime=False) as g:
        src.start(g, passthrough=False, pace=False)
        pkts = drain(src)
        assert g.flush(timeout=30)
        outs = g.read(timeout=0)
    assert pkts and all(p.stream == "audio" for p in pkts)
    assert [(o.group, o.object) for o in outs] == [(i // 30, i % 30) for i in range(60)]
    path = tmp_path / "rung.h264"
    path.write_bytes(b"".join(o.data for o in outs))
    frames = probe_frames(path)
    assert len(frames) == 60 and {(f["width"], f["height"]) for f in frames} == {(160, 90)}
    assert [i for i, f in enumerate(frames) if f["key_frame"]] == [0, 30]


def test_closing_the_graph_closes_its_source(tmp_path):
    clip = make_clip(tmp_path, "mp4")
    src = Source(str(clip))
    g = ladder([Rung(160, 90, 150_000)], profile="cpu", source=src)
    src.start(g)
    g.close()
    with pytest.raises(RuntimeError):
        src.stats()


def test_a_file_is_paced_to_its_timestamps_by_default(tmp_path):
    clip = make_clip(tmp_path, "mp4", seconds=1)
    took = {}
    for pace in (None, False):
        with Source(str(clip)) as src:
            t = time.monotonic()
            src.start(pace=pace)
            drain(src)
            took[pace] = time.monotonic() - t
    assert took[None] > 0.8 and took[False] < 0.5


def test_a_bad_input_raises(tmp_path):
    with pytest.raises(OSError):
        Source(str(tmp_path / "missing.mp4"))
    clip = make_clip(tmp_path, "mp4")
    with pytest.raises(OSError, match="unknown option"):
        Source(str(clip), {"no_such_option": "1"})


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def needs(name):
    have = cymedia.have_format if name in ("rtsp", "hls") else cymedia.have_protocol
    if not have(name):
        pytest.skip(f"libavformat has no {name}")


def listen_and_send(clip, url, options, send):
    """Opens a listening Source, starts an ffmpeg sender at it, and returns both."""
    if not shutil.which("ffmpeg"):
        pytest.skip("needs the ffmpeg command")
    with ThreadPoolExecutor(1) as ex:
        fut = ex.submit(Source, url, options, timeout=20)
        proc = None
        for _ in range(20):
            time.sleep(0.5)
            proc = subprocess.Popen(["ffmpeg", "-v", "error", "-re", "-stream_loop", "-1",
                                     "-i", str(clip), "-c", "copy", *send],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            time.sleep(0.5)
            if proc.poll() is None or fut.done():
                break
        try:
            return fut.result(timeout=20), proc
        except BaseException:
            proc.kill()
            proc.wait()
            raise


LIVE = {
    "srt": lambda port: (f"srt://127.0.0.1:{port}?mode=listener", None,
                         ["-f", "mpegts", f"srt://127.0.0.1:{port}?mode=caller"]),
    "rtmp": lambda port: (f"rtmp://127.0.0.1:{port}/live/test", {"listen": "1"},
                          ["-f", "flv", f"rtmp://127.0.0.1:{port}/live/test"]),
    "rtsp": lambda port: (f"rtsp://127.0.0.1:{port}/test",
                          {"rtsp_flags": "listen", "listen_timeout": "20"},
                          ["-f", "rtsp", "-rtsp_transport", "tcp",
                           f"rtsp://127.0.0.1:{port}/test"]),
}


@pytest.mark.parametrize("proto", sorted(LIVE))
def test_a_live_input_is_received(tmp_path, proto):
    needs(proto)
    clip = make_clip(tmp_path, "mp4")
    port = free_port(socket.SOCK_DGRAM if proto == "srt" else socket.SOCK_STREAM)
    url, options, send = LIVE[proto](port)
    src, proc = listen_and_send(clip, url, options, send)
    try:
        with src:
            assert src.video.codec == "h264" and src.video.extradata[0] == 1
            src.start()
            pkts = drain(src, video=60)
    finally:
        proc.kill()
        proc.wait()
    video = [p for p in pkts if p.stream == "video"]
    assert len(video) >= 60
    groups = {}
    for p in video:
        groups.setdefault(p.group, []).append(p)
    for items in groups.values():
        assert items[0].key and [p.object for p in items] == list(range(len(items)))
        assert all(9 not in nal_units(p.data) for p in items)
    check_audio([p for p in pkts if p.stream == "audio"])


def test_an_hls_playlist_is_read(tmp_path):
    needs("hls")
    clip = make_clip(tmp_path, "mp4")
    site = tmp_path / "hls"
    site.mkdir()
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(clip), "-c", "copy", "-f", "hls",
                        "-hls_time", "1", "-hls_list_size", "0", "-hls_playlist_type", "vod",
                        str(site / "index.m3u8")], capture_output=True)
    assert r.returncode == 0, r.stderr.decode()[-200:]

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                             functools.partial(Quiet, directory=str(site)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with Source(f"http://127.0.0.1:{server.server_address[1]}/index.m3u8") as src:
            src.start(pace=False)
            pkts = drain(src)
    finally:
        server.shutdown()
    assert src.ended and src.error is None
    check_video([p for p in pkts if p.stream == "video"], 60)
    check_audio([p for p in pkts if p.stream == "audio"])


def test_opening_a_listener_times_out():
    needs("srt")
    t = time.monotonic()
    with pytest.raises(OSError):
        Source(f"srt://127.0.0.1:{free_port(socket.SOCK_DGRAM)}?mode=listener", timeout=0.5)
    assert time.monotonic() - t < 5


def test_close_interrupts_a_blocked_read(tmp_path):
    needs("rtmp")
    clip = make_clip(tmp_path, "mp4")
    port = free_port()
    url, options, send = LIVE["rtmp"](port)
    src, proc = listen_and_send(clip, url, options, send)
    try:
        src.start()
        assert drain(src, video=10)
        proc.send_signal(signal.SIGSTOP)
        time.sleep(0.5)
        t = time.monotonic()
        src.close()
        assert time.monotonic() - t < 2
    finally:
        proc.kill()
        proc.wait()
