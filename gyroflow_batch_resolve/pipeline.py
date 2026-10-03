"""Match recorder clips to gyro takes and write a .gyroflow beside each clip.

A take is `<name>.GYR` + `<name>.json` from the fpSup Base build with the HDMI
hooks. A take and a clip do not start together when the recorder was started
or stopped from its own screen (the camera keeps logging), so each clip is
located inside the takes: by timecode when the camera wrote one into the
take's .json (the recorder stamps the same timecode on the clip), otherwise by
cross-correlating the video's frame-to-frame motion with the gyro's angular
rate.

When a clip's timecode falls inside more than one take (the timecode carries
no date, so takes from different days can overlap), the file dates pick the
take, and failing that a few seconds of the clip are decoded and compared with
each candidate at its timecode position.

The .gyroflow is written by the Gyroflow CLI with the gyro data embedded
(--export-project 2), so it needs no other file. The Gyroflow OFX plugin in
Resolve loads `<clip>.gyroflow` from the clip's folder by itself.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from . import gyr, tools

VIDEO_EXTS = {".mov", ".mp4", ".mxf"}

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
# Timecode has no date. When it puts a clip inside several takes, the take
# whose .json mtime (the log opening, camera clock) is nearest the clip's
# creation_time (recorder clock) is used if it is this much nearer than every
# other. The two clocks were 45-75 min apart on the test rig.
DATE_MARGIN_S = 12 * 3600
# Seconds decoded from the middle of the clip to choose between takes that
# timecode and dates leave tied: 6 s of 4K ProRes RAW decodes in about 10 s.
SAMPLE_S = 6.0

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
    opened: float | None = None  # .json mtime, epoch s: when the log opened


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
    created: float | None = None  # the container's creation_time, epoch s
    status: str = "pending"

    @property
    def project(self) -> Path:
        return self.path.with_suffix(".gyroflow")


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
            takes.append(Take(p, lens, gyr.read(p), lens_timecode(lens), lens.stat().st_mtime))
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
    found = folder.rglob("*") if subfolders else folder.glob("*")
    return [Clip(p) for p in sorted(found)
            if p.suffix.lower() in VIDEO_EXTS and not p.name.startswith("._")]


def probe_info(clip: Clip, t: Tools) -> None:
    """Size, frame rate and duration from the container. Fast: nothing is decoded."""
    info = json.loads(tools.run([t.ffprobe, "-v", "error", "-select_streams", "v:0", "-print_format", "json",
                                 "-show_entries",
                                 "stream=width,height,r_frame_rate:stream_tags=timecode"
                                 ":format=duration:format_tags=timecode,creation_time",
                                 clip.path]).stdout)
    stream = info["streams"][0]
    num, den = stream["r_frame_rate"].split("/")
    clip.width, clip.height, clip.fps = stream["width"], stream["height"], int(num) / int(den)
    clip.duration_s = float(info["format"]["duration"])
    clip.tc = (stream.get("tags", {}).get("timecode")
               or info["format"].get("tags", {}).get("timecode"))
    clip.created = parse_time(info["format"].get("tags", {}).get("creation_time"))


def parse_time(iso: str | None) -> float | None:
    try:
        # Python 3.10 does not accept a trailing "Z".
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def probe_motion(clip: Clip, t: Tools, start_s: float = 0.0, length_s: float | None = None) -> np.ndarray:
    """Decode the clip small and measure the change between frames. Slow: the
    ProRes RAW decoder is single-threaded, about 1.7 s per second of 4K on an
    M1 Pro. start_s and length_s limit the decode to part of the clip."""
    span = ["-ss", f"{start_s:.6f}"] + (["-t", f"{length_s:.6f}"] if length_s else [])
    res = tools.run([t.ffmpeg, "-v", "error", *span, "-i", clip.path,
                     "-vf", f"scale={PROBE_W}:{PROBE_H},format=gray", "-f", "rawvideo", "-"])
    if res.returncode != 0:
        raise tools.ToolError(f"ffmpeg could not decode {clip.path.name}: {res.stderr.decode(errors='replace')[:300]}")
    frames = np.frombuffer(res.stdout, dtype=np.uint8)
    frames = frames[: len(frames) // (PROBE_W * PROBE_H) * PROBE_W * PROBE_H].reshape(-1, PROBE_W * PROBE_H)
    return np.abs(np.diff(frames.astype(np.int16), axis=0)).mean(axis=1)


def rate_per_frame(cap: gyr.Capture, fps: float) -> np.ndarray:
    """Mean |angular rate| over each frame interval, from the take's start."""
    mag = np.linalg.norm(cap.gyro.astype(np.float64), axis=1)
    per = 1 / fps / cap.period_s
    edges = (np.arange(int(len(mag) / per) + 1) * per).astype(np.int64)
    sums = np.add.reduceat(mag, edges[:-1]) if len(edges) > 1 else np.array([])
    return sums / np.diff(edges)


def tc_hits(clip: Clip, takes: list[Take]) -> list[tuple[Take, float]]:
    """The takes whose timecode span holds the clip, with the clip's start in
    each, in seconds.

    Needs the camera on Free Run: the take's timecode and the clip's then come
    from one running clock, and one frame of timecode is one frame of time.
    With Rec Run the timecode stands still between takes and several takes can
    claim the clip."""
    if not clip.tc or not clip.fps:
        return []
    c = tc_frames(clip.tc, clip.fps)
    if c is None:
        return []
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
    return by_date(clip, hits) if len(hits) > 1 else hits


def by_date(clip: Clip, hits: list[tuple[Take, float]]) -> list[tuple[Take, float]]:
    """The one hit whose log opened nearest the clip's creation time, if it is
    DATE_MARGIN_S nearer than the rest; otherwise all of them."""
    if clip.created is None or any(take.opened is None for take, _ in hits):
        return hits
    ranked = sorted(hits, key=lambda h: abs(clip.created - (h[0].opened + h[1])))
    gap = [abs(clip.created - (take.opened + off)) for take, off in ranked[:2]]
    return ranked[:1] if gap[1] - gap[0] >= DATE_MARGIN_S else hits


def place_by_tc(clip: Clip, take: Take, off: float, corr: float = 1.0) -> None:
    # A negative start (the recorder started before the log opened) is kept:
    # gyr.gcsv pads the gap, so the clip and the gyro stay aligned.
    clip.take, clip.start_s, clip.corr, clip.method = take, off, corr, "timecode"


def locate_by_tc(clip: Clip, takes: list[Take]) -> bool:
    """Place the clip from timecodes alone.  True if exactly one take holds it."""
    hits = tc_hits(clip, takes)
    if len(hits) != 1:
        return False
    place_by_tc(clip, *hits[0])
    return True


def choose_by_sample(clip: Clip, hits: list[tuple[Take, float]], motion: np.ndarray, sample_s: float) -> bool:
    """Place the clip in the hit whose gyro best follows `motion`, the motion
    of the clip from sample_s on, searched within TC_SLACK_S of the timecode
    position. True if the best correlation reaches MIN_CORR."""
    slack = round(TC_SLACK_S * clip.fps)
    best = None
    for take, off in hits:
        at = round((off + sample_s) * clip.fps)
        _, r = best_fit(motion, rate_per_frame(take.cap, clip.fps), at - slack, at + slack)
        if best is None or r > best[2]:
            best = (take, off, r)
    if best is None or best[2] < MIN_CORR:
        return False
    place_by_tc(clip, *best)
    return True


def best_fit(m: np.ndarray, rate: np.ndarray, first: int, last: int) -> tuple[int, float]:
    """The start frame s in [first, last] (clamped to the take) where the
    motion m best correlates with rate, and the correlation; (0, 0.0) if none.
    m[i] pairs with rate[s + 1 + i]; see locate()."""
    n = len(m)
    first, last = max(first, 0), min(last, len(rate) - n - 1)
    if n < 2 or m.std() == 0 or last < first:
        return 0, 0.0
    mz = (m - m.mean()) / m.std()
    win = np.lib.stride_tricks.sliding_window_view(rate[first + 1:last + 1 + n], n)
    sd = win.std(axis=1)
    valid = sd > 0
    r = np.zeros(len(win))
    r[valid] = ((win[valid] - win[valid].mean(axis=1, keepdims=True)) @ mz) / (n * sd[valid])
    best = int(r.argmax())
    return first + best, float(r[best])


def locate(clip: Clip, takes: list[Take]) -> None:
    """Set clip.take / start_s / corr to the best placement inside any take.

    Only placements with the whole clip inside the take are tried: the take
    starts at the REC press, which cannot come after the recorder started.
    motion[i] is the change from frame i to i+1, so it pairs with the gyro over
    frame interval i+1.
    """
    m = clip.motion
    for take in takes:
        rate = rate_per_frame(take.cap, clip.fps)
        best, r = best_fit(m, rate, 0, len(rate))
        if r > clip.corr:
            clip.take, clip.start_s, clip.corr = take, best / clip.fps, r
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
    hits = tc_hits(clip, takes)
    if len(hits) == 1:
        place_by_tc(clip, *hits[0])
        return MATCHED
    if hits:
        # Start on a whole frame, so the sample's first frame is known.
        sample_s = max(0, round((clip.duration_s - SAMPLE_S) / 2 * clip.fps)) / clip.fps
        step("decoding a sample")
        motion = probe_motion(clip, t, sample_s, SAMPLE_S)
        step("matching")
        if choose_by_sample(clip, hits, motion, sample_s):
            return MATCHED
    # A log starts at the REC press and stops at the stop, so it is at least as
    # long as any clip inside it. 1 s of slack for container rounding.
    if not takes or clip.duration_s > max(k.cap.duration_s for k in takes) + 1.0:
        return NO_GYRO
    step("decoding")
    clip.motion = probe_motion(clip, t)
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


def generate(clip: Clip, t: Tools) -> list[float]:
    """Write clip.project. Returns the autosync offsets in ms."""
    with tempfile.TemporaryDirectory() as tmp:
        gcsv = Path(tmp) / f"{clip.path.stem}.gcsv"
        lens = Path(tmp) / f"{clip.path.stem}.json"
        gcsv.write_text(gyr.gcsv(clip.take.cap, round(clip.start_s / clip.take.cap.period_s)))
        lens.write_text(json.dumps(lens_for(clip, clip.take.lens), indent=1))
        res = tools.run([t.gyroflow, clip.path, lens, "-g", gcsv, "--export-project", "2", "-f"],
                        timeout=GYROFLOW_TIMEOUT_S)
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
