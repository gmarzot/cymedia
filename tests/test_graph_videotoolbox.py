import pytest
from helpers import make_source, nal_types, probe_frames, run_graph

import cymedia
from cymedia import Rung, ladder


@pytest.mark.skipif(
    not (cymedia.have_encoder("h264_videotoolbox") and cymedia.have_filter("scale_vt")
         and cymedia.hw_available("videotoolbox")),
    reason="needs VideoToolbox H.264 and scale_vt")
def test_videotoolbox_ladder_keeps_ids_and_aligns_keyframes(tmp_path):
    aus = make_source(tmp_path, width=1280, height=720, frames=120, gop=60, rate=60)
    sizes = [(640, 360), (320, 180)]
    with ladder([Rung(640, 360, 900_000), Rung(320, 180, 300_000)], profile="videotoolbox",
                fps=(60, 1), realtime=False) as g:
        by = run_graph(g, aus, 60, fps=60)
    assert sorted(by) == [0, 1]
    for b, outs in by.items():
        assert [(o.group, o.object) for o in outs] == [(i // 60, i % 60) for i in range(120)]
        assert [o.key for o in outs] == [i % 60 == 0 for i in range(120)]
        assert not any(12 in nal_types(o.data) for o in outs)
        path = tmp_path / f"branch{b}.h264"
        path.write_bytes(b"".join(o.data for o in outs))
        frames = probe_frames(path)
        assert len(frames) == 120
        assert {(f["width"], f["height"]) for f in frames} == {sizes[b]}


def test_videotoolbox_profile_is_h264_only():
    with pytest.raises(ValueError, match="videotoolbox"):
        ladder([Rung(320, 180, 300_000)], profile="videotoolbox", codec="av1")
