"""Match recorder clips to gyro takes and write a .gyroflow beside each clip.

A take is `<name>.GYR` + `<name>.json` from the fpSup Base build with the HDMI
hooks. A take and a clip do not start together when the recorder was started
or stopped from its own screen (the camera keeps logging), so each clip is
located inside the takes: by timecode when the camera wrote one into the
take's .json (the recorder stamps the same timecode on the clip), otherwise by
cross-correlating the video's frame-to-frame motion with the gyro's angular
rate.

A CinemaDNG clip recorded in the camera needs no matching: the gcsv edition of
fpSup writes `<clip>.gcsv` + `<clip>.json` into the clip's folder, beside the
frames, and the clip is that folder.

The .gyroflow is written by the Gyroflow CLI with the gyro data embedded
(--export-project 2), so it needs no other file. The Gyroflow OFX plugin in
Resolve loads `<clip>.gyroflow` from the clip's folder by itself.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from . import gyr, tools

VIDEO_EXTS = {".mov", ".mp4", ".mxf"}

# A CinemaDNG frame: A001_461_20260928_000001.DNG
FRAME_RE = re.compile(r"(.*?)(\d+)(\.dng)", re.IGNORECASE)

# Enough for global motion, and small enough that decoding is the cost.
PROBE_W, PROBE_H = 96, 54

# Below this the location is a guess. Real matches measured 0.84-0.97,
# unrelated signals about 0.1.
MIN_CORR = 0.5

# Timecode placement.  The log opens at the REC or shutter press and the
# recorder starts a few frames later or earlier, and a take log can end a few
# frames before its clip; this much of the clip may fall outside the log.
TC_SLACK_S = 0.5
# Autosync's search around a timecode placement.  Measured: autosync moved
# timecode-placed clips by -11 and +26 ms, inside one frame (42 ms at 24p);
# 0.25 s leaves six frames of room and keeps autosync off unrelated motion.
TC_SEARCH_S = 0.25

# Gyroflow 1.6.3 reads autosync settings from the lens profile only; the CLI's
# -s is not consulted. search_size is +/- seconds around the located start.
SYNC_SETTINGS = {
    "do_autosync": True,
    "max_sync_points": 5,
    "time_per_syncpoint": 1.5,
    "search_size": 1.0,
    "every_nth_frame": 1,
    "of_method": 2,
    "offset_method": 2,
    "pose_method": 0,
}

# For a CinemaDNG clip the settings go in the project file, which is read after
# the lens profile and after the defaults saved by the Gyroflow app.  Those
# defaults can have auto_sync_points on, which picked one point on the test
# clip; five evenly spaced points came out 6 ms apart.  CinemaDNG has no
# timecode and the log starts within half a second of the first frame, so the
# +/- 1 s search holds it; at 5 s one of the five points locked 1.8 s off.
SEQUENCE_SYNC_SETTINGS = dict(SYNC_SETTINGS, auto_sync_points=False)

# Gyroflow 1.6.3's CLI never exits when a file fails to load (fixed upstream
# after 1.6.3), so each clip gets a hard limit.
GYROFLOW_TIMEOUT_S = 900

# A good autosync puts its points within a few ms of each other (2.6-8.8 ms on
# the test clips); one that did not lock scatters them by hundreds.
SYNC_SPREAD_WARN_MS = 50.0

# Full Gyroflow output of a failed clip, so the error line has context.
LOG_DIR = Path.home() / ".gyroflow-batch-resolve" / "logs"


@dataclass
class Tools:
    ffmpeg: Path
    ffprobe: Path
    gyroflow: Path

    @classmethod
    def find(cls, ffmpeg=None, ffprobe=None, gyroflow=None) -> "Tools":
        return cls(tools.find("ffmpeg", ffmpeg), tools.find("ffprobe", ffprobe), tools.find_gyroflow(gyroflow))

    def problems(self) -> list[str]:
        """What would stop a run, found before one starts."""
        found = []
        for name, exe in (("ffmpeg", self.ffmpeg), ("ffprobe", self.ffprobe), ("Gyroflow", self.gyroflow)):
            if not Path(exe).exists():
                found.append(f"{name} not found at {exe}")
        if not found:
            res = tools.run([self.ffmpeg, "-hide_banner", "-decoders"], timeout=30)
            if b"prores_raw" not in res.stdout:
                found.append("This ffmpeg cannot decode ProRes RAW; version 8.0 or newer is needed")
        return found


def sync_warning(offsets: list[float], by_timecode: bool = False) -> str | None:
    if len(offsets) < 2:
        msg = f"only {len(offsets)} sync point" + ("" if len(offsets) == 1 else "s")
        if by_timecode:
            msg += "; the timecode placement stands, within a frame"
        return msg
    spread = offsets[-1] - offsets[0]
    if spread > SYNC_SPREAD_WARN_MS:
        return f"sync points {spread:.0f} ms apart"
    return None


@dataclass
class Take:
    path: Path
    lens: Path
    cap: gyr.Capture
    tc: str | None = None       # the camera's timecode when the log opened


@dataclass
class Clip:
    path: Path
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration_s: float = 0.0
    motion: np.ndarray | None = None
    take: Take | None = None
    start_s: float = 0.0
    corr: float = 0.0
    method: str = ""            # "timecode" or "motion", once located
    tc: str | None = None       # the clip's start timecode, from the container
    status: str = "pending"
    # A CinemaDNG clip: path is the frame pattern (A001_461_20260928_%06d.DNG),
    # which is how Gyroflow names an image sequence, and its own log and lens
    # profile lie beside the frames.
    gcsv: Path | None = None
    lens: Path | None = None
    first_frame: int = 0
    frames: int = 0

    @property
    def project(self) -> Path:
        if self.gcsv:
            # Named after the first frame: the plugin looks for a project that
            # starts with the name of the file the editor gives it, and for a
            # sequence it reduces Resolve's [000001-000366] to the first number.
            first = re.sub(r"%0(\d+)d", lambda m: f"{self.first_frame:0{int(m[1])}d}", self.path.name)
            return self.path.with_name(first).with_suffix(".gyroflow")
        return self.path.with_suffix(".gyroflow")

    @property
    def gyro(self) -> Path | None:
        """The gyro log the clip was matched to, if any."""
        return self.gcsv or (self.take.path if self.take else None)


# What match() decided for a clip.
DONE, NO_GYRO, MATCHED = "done", "no gyro data", "matched"


def has_project(clip: Clip) -> bool:
    """A .gyroflow counts as done only if autosync wrote offsets into it; a file
    left by an interrupted run has none and is redone."""
    try:
        return bool(json.loads(clip.project.read_text()).get("offsets"))
    except (OSError, ValueError, AttributeError):
        return False


def find_takes(folder: Path, log: Callable[[str], None] = print) -> list[Take]:
    takes = []
    for p in sorted(folder.rglob("*")):
        if p.suffix.upper() != ".GYR" or p.name.startswith("._"):
            continue
        lens = p.with_suffix(".json")
        if not lens.exists():
            log(f"skip {p.name}: no {lens.name} beside it")
            continue
        try:
            takes.append(Take(p, lens, gyr.read(p), lens_timecode(lens)))
        except ValueError as e:
            log(f"skip {p.name}: {e}")
    return takes


def lens_timecode(lens: Path) -> str | None:
    try:
        tc = json.loads(lens.read_text()).get("timecode")
    except (OSError, ValueError, AttributeError):
        return None
    return tc if isinstance(tc, str) else None


def tc_frames(tc: str, fps: float) -> int | None:
    """HH:MM:SS:FF (or ;FF) as frames since midnight, counted at the nominal
    rate: 24 for 23.976, 30 for 29.97.  Drop-frame is not handled."""
    try:
        h, m, sec, f = (int(x) for x in tc.replace(";", ":").split(":"))
    except ValueError:
        return None
    return ((h * 60 + m) * 60 + sec) * round(fps) + f


def find_clips(folder: Path, subfolders: bool = True) -> list[Clip]:
    found = sorted(folder.rglob("*") if subfolders else folder.glob("*"))
    clips = [Clip(p) for p in found
             if p.suffix.lower() in VIDEO_EXTS and not p.name.startswith("._")]
    for p in found:
        if p.suffix.lower() == ".gcsv" and not p.name.startswith("._"):
            clip = sequence_clip(p)
            if clip:
                clips.append(clip)
    return sorted(clips, key=lambda c: c.path)


def sequence_clip(gcsv: Path) -> Clip | None:
    """The CinemaDNG clip in the folder of a camera-written .gcsv, if there is one."""
    runs: dict[tuple[str, int, str], list[int]] = {}
    for p in gcsv.parent.iterdir():
        m = FRAME_RE.fullmatch(p.name)
        if m and not p.name.startswith("._"):
            runs.setdefault((m[1], len(m[2]), m[3]), []).append(int(m[2]))
    if not runs:
        return None
    (prefix, digits, ext), numbers = max(runs.items(), key=lambda r: len(r[1]))
    lens = gcsv.with_suffix(".json")
    return Clip(gcsv.parent / f"{prefix}%0{digits}d{ext}", gcsv=gcsv, lens=lens if lens.exists() else None,
                first_frame=min(numbers), frames=len(numbers))


def probe_sequence(clip: Clip) -> None:
    """Size and frame rate from the camera's lens profile: DNG frames carry no
    rate that Gyroflow or ffprobe read, and an image sequence defaults to 25."""
    if clip.lens is None:
        raise ValueError(f"no {clip.gcsv.with_suffix('.json').name} beside {clip.gcsv.name}")
    d = json.loads(clip.lens.read_text())
    clip.width, clip.height, clip.fps = d["calib_dimension"]["w"], d["calib_dimension"]["h"], float(d["fps"])
    clip.duration_s = clip.frames / clip.fps


def probe_info(clip: Clip, t: Tools) -> None:
    """Size, frame rate and duration from the container. Fast: nothing is decoded."""
    if clip.gcsv:
        return probe_sequence(clip)
    info = json.loads(tools.run([t.ffprobe, "-v", "error", "-select_streams", "v:0", "-print_format", "json",
                                 "-show_entries",
                                 "stream=width,height,r_frame_rate:stream_tags=timecode"
                                 ":format=duration:format_tags=timecode",
                                 clip.path]).stdout)
    stream = info["streams"][0]
    num, den = stream["r_frame_rate"].split("/")
    clip.width, clip.height, clip.fps = stream["width"], stream["height"], int(num) / int(den)
    clip.duration_s = float(info["format"]["duration"])
    clip.tc = (stream.get("tags", {}).get("timecode")
               or info["format"].get("tags", {}).get("timecode"))


def probe_motion(clip: Clip, t: Tools) -> None:
    """Decode the clip small and measure the change between frames. Slow."""
    res = tools.run([t.ffmpeg, "-v", "error", "-i", clip.path,
                     "-vf", f"scale={PROBE_W}:{PROBE_H},format=gray", "-f", "rawvideo", "-"])
    if res.returncode != 0:
        raise tools.ToolError(f"ffmpeg could not decode {clip.path.name}: {res.stderr.decode(errors='replace')[:300]}")
    frames = np.frombuffer(res.stdout, dtype=np.uint8)
    frames = frames[: len(frames) // (PROBE_W * PROBE_H) * PROBE_W * PROBE_H].reshape(-1, PROBE_W * PROBE_H)
    clip.motion = np.abs(np.diff(frames.astype(np.int16), axis=0)).mean(axis=1)


def rate_per_frame(cap: gyr.Capture, fps: float) -> np.ndarray:
    """Mean |angular rate| over each frame interval, from the take's start."""
    mag = np.linalg.norm(cap.gyro.astype(np.float64), axis=1)
    per = 1 / fps / cap.period_s
    edges = (np.arange(int(len(mag) / per) + 1) * per).astype(np.int64)
    sums = np.add.reduceat(mag, edges[:-1]) if len(edges) > 1 else np.array([])
    return sums / np.diff(edges)


def locate_by_tc(clip: Clip, takes: list[Take]) -> bool:
    """Place the clip from timecodes alone.  True if exactly one take holds it.

    Needs the camera on Free Run: the take's timecode and the clip's then come
    from one running clock, and one frame of timecode is one frame of time.
    With Rec Run the timecode stands still between takes, several takes can
    claim the clip, and it is left to motion matching."""
    if not clip.tc or not clip.fps:
        return False
    c = tc_frames(clip.tc, clip.fps)
    if c is None:
        return False
    day = 24 * 3600 * round(clip.fps)
    hits = []
    for take in takes:
        k = tc_frames(take.tc, clip.fps) if take.tc else None
        if k is None:
            continue
        d = (c - k) % day
        if d > day // 2:
            d -= day                    # the clip starts before the log (or past midnight)
        off = d / clip.fps
        if -TC_SLACK_S <= off and off + clip.duration_s <= take.cap.duration_s + TC_SLACK_S:
            hits.append((take, off))
    if len(hits) != 1:
        return False
    clip.take, off = hits[0]
    # A negative start (the recorder started before the log opened) is kept:
    # gyr.gcsv pads the gap, so the clip and the gyro stay aligned.
    clip.start_s, clip.corr, clip.method = off, 1.0, "timecode"
    return True


def locate(clip: Clip, takes: list[Take]) -> None:
    """Set clip.take / start_s / corr to the best placement inside any take.

    Only placements with the whole clip inside the take are tried: the take
    starts at the REC press, which cannot come after the recorder started.
    motion[i] is the change from frame i to i+1, so it pairs with the gyro over
    frame interval i+1.
    """
    m = clip.motion
    n = len(m)
    if n < 2 or m.std() == 0:
        return
    mz = (m - m.mean()) / m.std()
    for take in takes:
        rate = rate_per_frame(take.cap, clip.fps)
        if len(rate) < n + 1:
            continue
        win = np.lib.stride_tricks.sliding_window_view(rate[1:], n)
        sd = win.std(axis=1)
        valid = sd > 0
        r = np.zeros(len(win))
        r[valid] = ((win[valid] - win[valid].mean(axis=1, keepdims=True)) @ mz) / (n * sd[valid])
        best = int(r.argmax())
        if r[best] > clip.corr:
            clip.take, clip.start_s, clip.corr = take, best / clip.fps, float(r[best])
            clip.method = "motion"


def match(clip: Clip, takes: list[Take], t: Tools, redo: bool = False,
          step: Callable[[str], None] = lambda s: None) -> str:
    """Decide what to do with a clip: DONE, NO_GYRO or MATCHED (clip.take set).

    Cheapest checks first: an existing project, then the container, and only
    then the decode that matching needs. Raises ToolError if a tool fails.
    `step` is told which stage is running, for progress display."""
    if not redo and has_project(clip):
        return DONE
    step("reading")
    probe_info(clip, t)
    if clip.gcsv:
        clip.corr, clip.method = 1.0, "own log"
        return MATCHED
    if locate_by_tc(clip, takes):
        return MATCHED
    # A log starts at the REC press and stops at the stop, so it is at least as
    # long as any clip inside it. 1 s of slack for container rounding.
    if not takes or clip.duration_s > max(k.cap.duration_s for k in takes) + 1.0:
        return NO_GYRO
    step("decoding")
    probe_motion(clip, t)
    step("matching")
    locate(clip, takes)
    if clip.take is None or clip.corr < MIN_CORR:
        clip.take = None
        return NO_GYRO
    return MATCHED


def lens_for(clip: Clip, source: Path) -> dict:
    """The camera's profile, corrected to the clip.

    On HDMI takes the camera writes the profile of the mode it monitors in
    (3856x2170 @59.94), not what the recorder stored. The recorded frame is a
    centre crop of that readout, so fx and fy are kept and the principal point
    moves to the clip centre. frame_readout_time is kept as written."""
    d = json.loads(source.read_text())
    w, h = clip.width, clip.height
    for k in ("calib_dimension", "orig_dimension", "output_dimension"):
        d[k] = {"w": w, "h": h}
    m = d.get("fisheye_params", {}).get("camera_matrix")
    if m:
        m[0][2], m[1][2] = w / 2, h / 2
    d["fps"] = clip.fps
    d["camera_setting"] = f"{w}x{h} @{clip.fps:.3f} HDMI RAW"
    d["sync_settings"] = (dict(SYNC_SETTINGS, search_size=TC_SEARCH_S)
                          if clip.method == "timecode" else SYNC_SETTINGS)
    return d


def sequence_project(clip: Clip) -> dict:
    """The project Gyroflow is started from for a CinemaDNG clip.

    The CLI takes an image sequence only this way: given the frame pattern as
    a video it reads it at 25 fps, and the rate cannot be set from the command
    line.  video_info and output have to be there, or Gyroflow 1.6.3 stops with
    "Invalid duration_ms 0" or a panic in cli.rs.  The exported project is
    named after output_filename, less the suffix.

    The output size and the rolling-shutter readout are set here because the
    app sets them when a clip and a lens profile are opened by hand, and
    Gyroflow copies whatever the project holds: with the 0x0 output it writes
    otherwise, the plugin in Resolve shows a white frame and can crash."""
    ms = clip.duration_s * 1000
    lens = json.loads(clip.lens.read_text())
    return {
        "title": "Gyroflow data file", "version": 3,
        "videofile": clip.path.as_uri(),
        "image_sequence_start": clip.first_frame, "image_sequence_fps": clip.fps,
        "video_info": {"width": clip.width, "height": clip.height, "rotation": 0.0, "num_frames": clip.frames,
                       "fps": clip.fps, "duration_ms": ms, "vfr_fps": clip.fps, "vfr_duration_ms": ms},
        "calibration_data": lens,
        "gyro_source": {"filepath": clip.gcsv.as_uri()},
        "offsets": {},
        "synchronization": SEQUENCE_SYNC_SETTINGS,
        "stabilization": {"frame_readout_time": lens.get("frame_readout_time", 0.0),
                          "frame_readout_direction": lens.get("frame_readout_direction", "TopToBottom")},
        "output": {"output_filename": f"{clip.project.stem}_stabilized.mp4",
                   "output_folder": clip.path.parent.as_uri() + "/",
                   "output_width": clip.width, "output_height": clip.height},
    }


def generate(clip: Clip, t: Tools) -> list[float]:
    """Write clip.project. Returns the autosync offsets in ms."""
    if clip.gcsv:
        # Gyroflow checks that a file with the pattern's own name exists before
        # it opens the sequence; an empty one is enough.
        placeholder = None if clip.path.exists() else clip.path
        with tempfile.TemporaryDirectory() as tmp:
            start = Path(tmp) / f"{clip.gcsv.stem}.gyroflow"
            start.write_text(json.dumps(sequence_project(clip), indent=1))
            try:
                if placeholder:
                    placeholder.touch()
                res = tools.run([t.gyroflow, start, "--export-project", "2", "-f"], timeout=GYROFLOW_TIMEOUT_S)
            finally:
                if placeholder:
                    placeholder.unlink(missing_ok=True)
        return _offsets(clip, res)
    with tempfile.TemporaryDirectory() as tmp:
        gcsv = Path(tmp) / f"{clip.path.stem}.gcsv"
        lens = Path(tmp) / f"{clip.path.stem}.json"
        gcsv.write_text(gyr.gcsv(clip.take.cap, round(clip.start_s / clip.take.cap.period_s)))
        lens.write_text(json.dumps(lens_for(clip, clip.take.lens), indent=1))
        res = tools.run([t.gyroflow, clip.path, lens, "-g", gcsv, "--export-project", "2", "-f"],
                        timeout=GYROFLOW_TIMEOUT_S)
    return _offsets(clip, res)


def _offsets(clip: Clip, res) -> list[float]:
    """The sync offsets Gyroflow wrote into clip.project; ToolError if it did not."""
    out = (res.stdout + res.stderr).decode(errors="replace")
    if res.returncode != 0 or not clip.project.exists():
        errors = [l for l in out.splitlines() if "ERROR" in l and "bookmark" not in l]
        reason = errors[-1] if errors else f"Gyroflow exited with {res.returncode}"
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            log = LOG_DIR / f"{clip.path.stem}.log"
            log.write_text(out)
            reason += f" (full output: {log})"
        except OSError:
            pass
        raise tools.ToolError(reason)
    return sorted(json.loads(clip.project.read_text()).get("offsets", {}).values())
