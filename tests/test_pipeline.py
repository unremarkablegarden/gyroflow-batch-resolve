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


def test_find_clips_subfolders(tmp_path):
    (tmp_path / "sub").mkdir()
    for name in ("A.mov", "._A.mov", "notes.txt", "sub/B.MOV"):
        (tmp_path / name).touch()
    names = lambda clips: [c.path.relative_to(tmp_path).as_posix() for c in clips]
    assert names(pipeline.find_clips(tmp_path)) == ["A.mov", "sub/B.MOV"]
    assert names(pipeline.find_clips(tmp_path, subfolders=False)) == ["A.mov"]


def tc_take(duration_s, tc):
    n = round(duration_s * 2499.466)
    cap = gyr.parse(v8_take([(0, 0, 0)] * n), "H")
    return pipeline.Take(Path("H.GYR"), Path("H.json"), cap, tc)


def test_tc_frames_counts_at_the_nominal_rate():
    assert pipeline.tc_frames("03:53:21:12", 24.0) == ((3 * 60 + 53) * 60 + 21) * 24 + 12
    assert pipeline.tc_frames("00:00:01;00", 29.97) == 30
    assert pipeline.tc_frames("garbage", 24.0) is None


def test_locate_by_tc_picks_the_log_the_clip_started_in():
    # An idle log, then the take log that the start press opened; the clip
    # starts at the second log's timecode and runs 2 frames past its end.
    idle = tc_take(9.35, "03:53:12:09")
    take = tc_take(4.74, "03:53:21:12")
    clip = pipeline.Clip(Path("T001.mov"), 3840, 2160, 24.0, duration_s=4.79, tc="03:53:21:12")
    assert pipeline.locate_by_tc(clip, [idle, take])
    assert clip.take is take and clip.start_s == 0.0 and clip.method == "timecode"


def test_locate_by_tc_places_a_clip_inside_a_session_log():
    log = tc_take(60.0, "10:00:00:00")
    clip = pipeline.Clip(Path("c.mov"), 3840, 2160, 24.0, duration_s=5.0, tc="10:00:12:12")
    assert pipeline.locate_by_tc(clip, [log])
    assert abs(clip.start_s - 12.5) < 1e-9


def test_locate_by_tc_leaves_ambiguous_or_missing_timecodes_to_motion():
    # Rec Run: two logs opened at the same stopped timecode.
    a, b = tc_take(10.0, "01:00:00:00"), tc_take(10.0, "01:00:00:00")
    clip = pipeline.Clip(Path("c.mov"), 3840, 2160, 24.0, duration_s=3.0, tc="01:00:02:00")
    assert not pipeline.locate_by_tc(clip, [a, b]) and clip.take is None
    assert not pipeline.locate_by_tc(pipeline.Clip(Path("c.mov"), 3840, 2160, 24.0, duration_s=3.0),
                                     [tc_take(10.0, None)])


def test_gcsv_pads_a_clip_that_started_before_the_log():
    cap = gyr.parse(v8_take([(5, 6, 7), (8, 9, 10)], accel_at=[(0, (1, 2, 3))]), "T")
    lines = gyr.gcsv(cap, start_sample=-2).strip().split("\n")
    body = lines[lines.index("t,gx,gy,gz,ax,ay,az") + 1:]
    assert body == ["0,0,0,0,2,-1,3", "1,0,0,0,2,-1,3", "2,5,6,7,2,-1,3", "3,8,9,10,2,-1,3"]


def test_timecode_placement_narrows_the_autosync_search(tmp_path):
    src = tmp_path / "H.json"
    src.write_text(json.dumps({"calib_dimension": {"w": 3840, "h": 2160}}))
    clip = pipeline.Clip(Path("c.mov"), 3840, 2160, 24.0)
    assert pipeline.lens_for(clip, src)["sync_settings"]["search_size"] == pipeline.SYNC_SETTINGS["search_size"]
    clip.method = "timecode"
    assert pipeline.lens_for(clip, src)["sync_settings"]["search_size"] == pipeline.TC_SEARCH_S
    assert pipeline.SYNC_SETTINGS["search_size"] == 1.0      # the shared default is untouched


def cdng_clip(folder, frames=3, lens=True):
    """A CinemaDNG clip folder as the gcsv edition of fpSup leaves it."""
    folder.mkdir(parents=True)
    for i in range(1, frames + 1):
        (folder / f"{folder.name}_20260928_{i:06d}.DNG").touch()
    (folder / f"{folder.name}_20260928.WAV").touch()
    (folder / f"{folder.name}.gcsv").touch()
    if lens:
        (folder / f"{folder.name}.json").write_text(json.dumps(
            {"calib_dimension": {"w": 3464, "h": 2308}, "fps": 24.0, "frame_readout_time": 24.982}))


def test_find_clips_takes_a_cdng_folder_as_one_clip(tmp_path):
    cdng_clip(tmp_path / "A001_461")
    (tmp_path / "B.mov").touch()
    (tmp_path / "stray.gcsv").touch()       # a log with no frames beside it is not a clip
    clips = pipeline.find_clips(tmp_path)
    assert [c.path.relative_to(tmp_path).as_posix() for c in clips] == ["A001_461/A001_461_20260928_%06d.DNG", "B.mov"]
    seq = clips[0]
    assert seq.gcsv.name == "A001_461.gcsv" and seq.lens.name == "A001_461.json"
    assert (seq.first_frame, seq.frames) == (1, 3)
    assert seq.project.name == "A001_461_20260928_%06d.gyroflow"
    assert pipeline.find_clips(tmp_path, subfolders=False)[0].path.name == "B.mov"


def test_cdng_clip_is_matched_to_its_own_log(tmp_path):
    cdng_clip(tmp_path / "A001_461", frames=48)
    clip, = pipeline.find_clips(tmp_path)
    assert pipeline.match(clip, [], None) == pipeline.MATCHED
    assert (clip.width, clip.height, clip.fps, clip.duration_s) == (3464, 2308, 24.0, 2.0)
    project = pipeline.sequence_project(clip)
    assert project["videofile"].endswith("/A001_461/A001_461_20260928_%2506d.DNG")
    assert project["image_sequence_fps"] == 24.0 and project["image_sequence_start"] == 1
    assert project["video_info"]["duration_ms"] == 2000.0
    assert project["synchronization"]["auto_sync_points"] is False
    assert project["output"]["output_filename"] == "A001_461_20260928_%06d_stabilized.mp4"
    assert (project["output"]["output_width"], project["output"]["output_height"]) == (3464, 2308)
    assert project["stabilization"]["frame_readout_time"] == 24.982


def test_cdng_clip_without_a_lens_profile_fails(tmp_path):
    import pytest

    cdng_clip(tmp_path / "A001_461", lens=False)
    clip, = pipeline.find_clips(tmp_path)
    with pytest.raises(ValueError, match="no A001_461.json"):
        pipeline.match(clip, [], None)
