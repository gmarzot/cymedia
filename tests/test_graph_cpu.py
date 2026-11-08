import time

import pytest
from helpers import make_source, probe_frames, run_graph

import cymedia
from cymedia import Branch, Graph, Rung, ladder

STEP = 1_000_000 // 30


def cpu_ladder(rungs, **kw):
    kw.setdefault("realtime", False)
    return ladder(rungs, profile="cpu", fps=(30, 1), **kw)


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


def test_keyframes_follow_group_starts_not_source_idrs(tmp_path):
    aus = make_source(tmp_path, gop=30)
    with cpu_ladder([Rung(320, 180, 400_000)]) as g:
        outs = run_graph(g, aus, 15)[0]
    assert len(outs) == 90
    assert [o.key for o in outs] == [i % 15 == 0 for i in range(90)]


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
