import json
import struct
from pathlib import Path

import numpy as np

from gyroflow_batch_resolve import gyr, pipeline

PERIOD_PS = 400_085_400   # 2499.466 Hz, as the camera writes it


def v8_take(gyro, accel_at=(), orientation=b"xyz\0"):
    """A v8 .GYR: 20-byte header, then 8-byte records."""
    data = bytearray(struct.pack("<IIff4s", gyr.MAGIC_V8, PERIOD_PS, 0.000137923, 1 / 1024, orientation))
    accel = dict(accel_at)
    for i, (x, y, z) in enumerate(gyro):
        if i in accel:
            ax, ay, az = accel[i]
            data += struct.pack("<hhhh", ax, ay, gyr.TAG_ACCEL, az)
        data += struct.pack("<hhhh", x, y, gyr.TAG_GYRO, z)
    return bytes(data)


def test_v8_header_and_records():
    cap = gyr.parse(v8_take([(1, 2, 3), (4, 5, 6)], accel_at=[(1, (10, 20, 30))]), "H001_577")
    assert cap.layout == "v8" and cap.clip == "H001_577" and cap.orientation == "xyz"
    assert abs(1 / cap.period_s - 2499.466) < 0.01
    assert cap.gyro.tolist() == [[1, 2, 3], [4, 5, 6]]
    assert cap.accel.tolist() == [[10, 20, 30]] and cap.accel_after.tolist() == [1]


def test_gcsv_trims_and_renumbers_and_holds_accel():
    gyro = [(i, -i, 2 * i) for i in range(6)]
    cap = gyr.parse(v8_take(gyro, accel_at=[(0, (1, 2, 3)), (4, (7, 8, 9))]), "T")
    lines = gyr.gcsv(cap, start_sample=2).strip().split("\n")
    body = lines[lines.index("t,gx,gy,gz,ax,ay,az") + 1:]
    # t restarts at 0; accel (x, y, z) is written as (y, -x, z) and held.
    assert body == ["0,2,-2,4,2,-1,3", "1,3,-3,6,2,-1,3", "2,4,-4,8,8,-7,9", "3,5,-5,10,8,-7,9"]
    assert "tscale,0.0004000854" in lines


def test_lens_is_corrected_to_the_clip(tmp_path):
    src = tmp_path / "H.json"
    src.write_text(json.dumps({
        "calib_dimension": {"w": 3856, "h": 2170}, "fps": 59.94, "frame_readout_time": 6.16,
        "fisheye_params": {"camera_matrix": [[3100.0, 0, 1928.0], [0, 3100.0, 1085.0], [0, 0, 1]]},
    }))
    d = pipeline.lens_for(pipeline.Clip(Path("c.mov"), 3840, 2160, 24.0), src)
    assert d["calib_dimension"] == d["orig_dimension"] == {"w": 3840, "h": 2160}
    assert d["fps"] == 24.0 and d["frame_readout_time"] == 6.16
    assert d["fisheye_params"]["camera_matrix"][0] == [3100.0, 0, 1920.0]
    assert d["fisheye_params"]["camera_matrix"][1] == [0, 3100.0, 1080.0]
    assert d["sync_settings"]["do_autosync"] is True


def test_locate_finds_the_clip_inside_a_longer_take():
    rng = np.random.default_rng(1)
    fps, per_frame = 24.0, 104          # 2499.466 / 24 samples per frame, rounded
    rate = np.abs(rng.normal(0, 1, 2000)).repeat(per_frame)
    gyro = np.stack([rate * 1000, np.zeros_like(rate), np.zeros_like(rate)], 1).astype(np.int16)
    cap = gyr.Capture("v8", "T", 1 / fps / per_frame, 0.000137923, 1 / 1024, "xyz",
                      gyro, np.zeros((0, 3), np.int16), np.zeros(0, np.int64))
    start = 700
    clip = pipeline.Clip(Path("c.mov"), fps=fps)
    # The frame change i -> i+1 follows the rotation during frame i+1.
    clip.motion = rate[::per_frame][start + 1:start + 301] * 3.0 + 0.5
    pipeline.locate(clip, [pipeline.Take(Path("T.GYR"), Path("T.json"), cap)])
    assert clip.take is not None and clip.corr > 0.99
    assert round(clip.start_s * fps) == start


def test_sync_warning():
    assert pipeline.sync_warning([-43.7, -42.7, -41.4, -38.5, -35.0]) is None
    assert pipeline.sync_warning([-58.8, 1356.3]) == "sync points 1415 ms apart"
    assert pipeline.sync_warning([-58.8]) == "only 1 sync point"
    assert pipeline.sync_warning([]) == "only 0 sync points"


def test_cancel_kills_a_running_process():
    import sys
    import threading
    import time

    import pytest

    from gyroflow_batch_resolve import tools

    tools.reset_cancel()
    threading.Timer(0.5, tools.cancel).start()
    started = time.monotonic()
    with pytest.raises(tools.Cancelled):
        tools.run([sys.executable, "-c", "import time; time.sleep(30)"])
    assert time.monotonic() - started < 5
    with pytest.raises(tools.Cancelled):   # and nothing new starts until reset
        tools.run([sys.executable, "-c", "pass"])
    tools.reset_cancel()
    assert tools.run([sys.executable, "-c", "pass"]).returncode == 0
