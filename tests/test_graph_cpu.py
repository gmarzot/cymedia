import time

import pytest
from helpers import make_source, obu_types, probe_frames, run_graph, write_ivf

import cymedia
from cymedia import Branch, Graph, Rung, ladder

STEP = 1_000_000 // 30

needs_cpu_h264 = pytest.mark.skipif(
    not (cymedia.have_encoder("libx264") or cymedia.have_encoder("libopenh264")),
    reason="needs a software H.264 encoder")


def cpu_ladder(rungs, **kw):
    kw.setdefault("realtime", False)
    return ladder(rungs, profile="cpu", fps=(30, 1), **kw)


@needs_cpu_h264
def test_two_branches_keep_source_ids_and_align_keyframes(tmp_path):
    aus = make_source(tmp_path)
    sizes = [(320, 180), (160, 90)]
    with cpu_ladder([Rung(320, 180, 400_000), Rung(160, 90, 150_000)]) as g:
        by = run_graph(g, aus, 30)
        st = g.stats()
    assert sorted(by) == [0, 1]
    for b, outs in by.items():
        assert [(o.group, o.object, o.pts_us) for o in outs] == \
            [(i // 30, i % 30, i * STEP) for i in range(90)]
        assert [o.key for o in outs] == [i % 30 == 0 for i in range(90)]
        assert [o.group_start for o in outs] == [i % 30 == 0 for i in range(90)]
        path = tmp_path / f"branch{b}.h264"
        path.write_bytes(b"".join(o.data for o in outs))
        frames = probe_frames(path)
        assert len(frames) == 90
        assert {(f["width"], f["height"]) for f in frames} == {sizes[b]}
        assert [i for i, f in enumerate(frames) if f["key_frame"]] == [0, 30, 60]
    assert st["packets_in"] == 90 and st["frames_decoded"] == 90
    assert [(b["frames_out"], b["errors"]) for b in st["branches"]] == [(90, 0), (90, 0)]


@needs_cpu_h264
def test_keyframes_follow_group_starts_not_source_idrs(tmp_path):
    aus = make_source(tmp_path, gop=30)
    with cpu_ladder([Rung(320, 180, 400_000)]) as g:
        outs = run_graph(g, aus, 15)[0]
    assert len(outs) == 90
    assert [o.key for o in outs] == [i % 15 == 0 for i in range(90)]


@needs_cpu_h264
def test_realtime_drops_cut_groups_short_and_resume_at_a_group_start(tmp_path):
    aus = make_source(tmp_path)
    with cpu_ladder([Rung(320, 180, 400_000)], realtime=True, input_depth=2, queue_depth=1) as g:
        for i, au in enumerate(aus):
            if i % 30 == 0:
                time.sleep(0.3)  # lets the queues drain, so every group starts
            g.push(au, i // 30, i % 30, i * STEP, i % 30 == 0)
        assert g.flush(timeout=60)
        outs = g.read(timeout=0)
        st = g.stats()
    assert st["packets_in"] + st["packets_dropped"] == 90
    assert st["packets_dropped"] > 0
    groups = {}
    for o in outs:
        groups.setdefault(o.group, []).append(o)
    assert sorted(groups) == [0, 1, 2]
    for items in groups.values():
        assert items[0].key and items[0].group_start
        assert [o.object for o in items] == list(range(len(items)))


def _push_faster_than_decode(g, aus, gop, gap_s=0.00025):
    """Group 0, a pause while the encoders open, then the rest `gap_s` apart."""
    for i, au in enumerate(aus):
        if i == gop:
            time.sleep(0.5)
        elif i > gop:
            t = time.perf_counter() + gap_s
            while time.perf_counter() < t:
                pass
        g.push(au, i // gop, i % gop, i * STEP, i % gop == 0)
    assert g.flush(timeout=60)
    return g.read(timeout=0), g.stats()


@needs_cpu_h264
def test_a_packet_that_waited_input_max_ms_is_dropped_with_its_group(tmp_path, caplog):
    aus = make_source(tmp_path, 1920, 1080, frames=120)
    with cpu_ladder([Rung(320, 180, 400_000)], realtime=True, input_depth=1000, queue_depth=64,
                    input_max_ms=10) as g:
        outs, st = _push_faster_than_decode(g, aus, 30)
    assert st["packets_dropped"] > 0
    assert st["packets_in"] + st["packets_dropped"] == 120
    assert "input waited over 10 ms" in caplog.text
    groups = {}
    for o in outs:
        groups.setdefault(o.group, []).append(o)
    for items in groups.values():
        assert items[0].key and items[0].group_start
        assert [o.object for o in items] == list(range(len(items)))


@needs_cpu_h264
def test_input_max_ms_zero_bounds_input_by_frames_only(tmp_path):
    aus = make_source(tmp_path, 1920, 1080, frames=120)
    with cpu_ladder([Rung(320, 180, 400_000)], realtime=True, input_depth=1000, queue_depth=64,
                    input_max_ms=0) as g:
        outs, st = _push_faster_than_decode(g, aus, 30)
    assert st["packets_dropped"] == 0
    assert [(o.group, o.object) for o in outs] == [(i // 30, i % 30) for i in range(120)]


@needs_cpu_h264
def test_a_late_frame_of_an_older_group_is_dropped(tmp_path):
    aus = make_source(tmp_path)
    order = list(range(29)) + [30, 31, 29] + list(range(32, 60))
    with cpu_ladder([Rung(320, 180, 400_000), Rung(160, 90, 150_000)]) as g:
        pushed = [g.push(aus[i], i // 30, i % 30, i * STEP, i % 30 == 0) for i in order]
        assert g.flush(timeout=60)
        outs = g.read(timeout=0)
        st = g.stats()
    assert [i for i, ok in zip(order, pushed) if not ok] == [29]
    for b in (0, 1):
        assert [(o.group, o.object) for o in outs if o.branch == b] == \
            [(i // 30, i % 30) for i in order if i != 29]
    assert st["packets_dropped"] == 1 and st["frames_decoded"] == 59


@needs_cpu_h264
def test_a_restarted_source_is_taken(tmp_path):
    aus = make_source(tmp_path)
    order = [(i // 30, i % 30, i) for i in range(90)] + [(0, i, i) for i in range(30)]
    with cpu_ladder([Rung(320, 180, 400_000), Rung(160, 90, 150_000)]) as g:
        pushed = [g.push(aus[a], grp, obj, n * STEP, obj == 0)
                  for n, (grp, obj, a) in enumerate(order)]
        assert g.flush(timeout=60)
        outs = g.read(timeout=0)
        st = g.stats()
    assert all(pushed) and st["packets_dropped"] == 0
    for b in (0, 1):
        assert [(o.group, o.object) for o in outs if o.branch == b] == \
            [(grp, obj) for grp, obj, _ in order]


@needs_cpu_h264
def test_native_sink_receives_every_frame(tmp_path):
    aus = make_source(tmp_path)
    sink = cymedia._core._counting_sink()
    g = cpu_ladder([Rung(320, 180, 400_000), Rung(160, 90, 150_000)], sink=sink)
    for i, au in enumerate(aus):
        g.push(au, i // 30, i % 30, i * STEP, i % 30 == 0)
    assert g.flush(timeout=60)
    with pytest.raises(RuntimeError):
        g.read(timeout=0)
    g.close()
    assert cymedia._core._counting_sink_frames(sink) == 180


@needs_cpu_h264
def test_push_after_flush_raises(tmp_path):
    aus = make_source(tmp_path)
    with cpu_ladder([Rung(320, 180, 400_000)]) as g:
        g.push(aus[0], 0, 0, 0, True)
        assert g.flush(timeout=60)
        assert g.flush(timeout=0)
        with pytest.raises(RuntimeError):
            g.push(aus[1], 0, 1, STEP, False)


def test_rejects_unknown_encoder():
    with pytest.raises(ValueError, match="unknown encoder"):
        Graph([Branch("", "no-such-encoder")])


def test_rejects_a_sink_that_is_not_a_capsule():
    with pytest.raises(TypeError):
        Graph([Branch("", "libx264")], sink=object())


def test_ffmpeg_info_reports_the_license():
    info = cymedia.ffmpeg_info()
    assert info["license"] and info["libavcodec"].count(".") == 2


@pytest.mark.skipif(not cymedia.have_encoder("libaom-av1"), reason="needs libaom")
def test_cpu_av1_ladder_keeps_ids_and_keys_at_group_starts(tmp_path):
    aus = make_source(tmp_path, frames=60, gop=30)
    sizes = [(320, 180), (160, 90)]
    with ladder([Rung(320, 180, 300_000), Rung(160, 90, 100_000)], profile="cpu", codec="av1",
                fps=(30, 1), realtime=False) as g:
        by = run_graph(g, aus, 30)
    assert sorted(by) == [0, 1]
    for b, outs in by.items():
        assert [(o.group, o.object) for o in outs] == [(i // 30, i % 30) for i in range(60)]
        assert [o.key for o in outs] == [i % 30 == 0 for i in range(60)]
        assert all(15 not in obu_types(o.data) for o in outs)
        path = tmp_path / f"branch{b}.ivf"
        write_ivf(path, [o.data for o in outs], *sizes[b], 30)
        frames = probe_frames(path)
        assert len(frames) == 60
        assert {(f["width"], f["height"]) for f in frames} == {sizes[b]}


def test_ladder_refuses_an_unknown_codec():
    with pytest.raises(ValueError):
        ladder([Rung(320, 180, 300_000)], profile="cpu", codec="vp9")


@pytest.mark.skipif(not cymedia.have_encoder("libaom-av1"), reason="needs libaom")
def test_an_encoder_that_will_not_open_counts_errors_without_output(tmp_path):
    aus = make_source(tmp_path, frames=30, gop=15)
    bad = Branch("scale=160:90,format=yuv420p", "libaom-av1", {"cpu-used": "99"})
    with Graph([bad], realtime=False, fps=(30, 1)) as g:
        by = run_graph(g, aus, 15)
        stats = g.stats()["branches"][0]
    assert by == {}
    assert stats["errors"] == 30

