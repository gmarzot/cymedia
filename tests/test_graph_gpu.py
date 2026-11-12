import pytest
from helpers import make_source, nal_types, obu_types, probe_frames, run_graph, write_ivf

import cymedia
from cymedia import Rung, ladder

pytestmark = pytest.mark.skipif(
    not (cymedia.have_encoder("h264_nvenc") and cymedia.have_filter("scale_cuda")
         and cymedia.hw_available("cuda")),
    reason="needs CUDA, NVENC and scale_cuda")

NOISE = 20


def test_nvenc_ladder_keeps_ids_aligns_keyframes_and_strips_filler(tmp_path):
    aus = make_source(tmp_path, width=1280, height=720, frames=120, gop=60, rate=60)
    sizes = [(640, 360), (320, 180)]
    with ladder([Rung(640, 360, 900_000), Rung(320, 180, 300_000)], profile="nvenc",
                fps=(60, 1), realtime=False) as g:
        by = run_graph(g, aus, 60, fps=60)
    step = 1_000_000 // 60
    assert sorted(by) == [0, 1]
    for b, outs in by.items():
        assert [(o.group, o.object, o.pts_us) for o in outs] == \
            [(i // 60, i % 60, i * step) for i in range(120)]
        assert [o.key for o in outs] == [i % 60 == 0 for i in range(120)]
        assert not any(12 in nal_types(o.data) for o in outs)
        path = tmp_path / f"branch{b}.h264"
        path.write_bytes(b"".join(o.data for o in outs))
        frames = probe_frames(path)
        assert len(frames) == 120
        assert {(f["width"], f["height"]) for f in frames} == {sizes[b]}


def test_nvenc_h264_rungs_keep_to_their_bit_rates(tmp_path):
    aus = make_source(tmp_path, width=1280, height=720, frames=240, gop=120, rate=60,
                      noise=NOISE)
    rungs = [Rung(1280, 720, 2_500_000), Rung(640, 360, 800_000)]
    with ladder(rungs, profile="nvenc", fps=(60, 1), realtime=False) as g:
        by = run_graph(g, aus, 120, fps=60)
    for b, rung in enumerate(rungs):
        bps = sum(len(o.data) for o in by[b]) * 8 * 60 / len(by[b])
        assert len(by[b]) == 240
        assert bps <= rung.bit_rate * 1.05, f"{rung}: {bps / 1e3:.0f} kbps"


@pytest.mark.skipif(not cymedia.have_encoder("av1_nvenc"), reason="needs AV1 NVENC")
def test_nvenc_av1_ladder_keeps_ids_aligns_keyframes_and_strips_padding(tmp_path):
    aus = make_source(tmp_path, width=1280, height=720, frames=120, gop=60, rate=60)
    sizes = [(640, 360), (320, 180)]
    with ladder([Rung(640, 360, 900_000), Rung(320, 180, 300_000)], profile="nvenc",
                codec="av1", fps=(60, 1), realtime=False) as g:
        by = run_graph(g, aus, 60, fps=60)
    assert sorted(by) == [0, 1]
    for b, outs in by.items():
        assert [(o.group, o.object) for o in outs] == [(i // 60, i % 60) for i in range(120)]
        assert [o.key for o in outs] == [i % 60 == 0 for i in range(120)]
        assert all(15 not in obu_types(o.data) for o in outs)
        assert 1 in obu_types(outs[0].data)
        path = tmp_path / f"branch{b}.ivf"
        write_ivf(path, [o.data for o in outs], *sizes[b], 60)
        frames = probe_frames(path)
        assert len(frames) == 120
        assert {(f["width"], f["height"]) for f in frames} == {sizes[b]}

