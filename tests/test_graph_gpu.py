import pytest
from helpers import make_source, nal_types, probe_frames, run_graph

import cymedia
from cymedia import Rung, ladder

pytestmark = pytest.mark.skipif(
    not (cymedia.have_encoder("h264_nvenc") and cymedia.have_filter("scale_cuda")
         and cymedia.hw_available("cuda")),
    reason="needs CUDA, NVENC and scale_cuda")


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
