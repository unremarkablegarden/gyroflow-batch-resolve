"""Command line: match and generate in one run.

    gyroflow-batch VIDEO_FOLDER --gyro GYRO_FOLDER [--dry-run] [--force] [--no-subfolders]
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from . import pipeline, tools


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gyroflow-batch", description=pipeline.__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("videos", type=Path, help="folder with the recorder clips (subfolders included unless --no-subfolders)")
    ap.add_argument("--gyro", type=Path, required=True, help="folder with the .GYR + .json takes")
    ap.add_argument("--dry-run", action="store_true", help="match only, write nothing")
    ap.add_argument("--force", action="store_true", help="redo clips that already have a synced .gyroflow")
    ap.add_argument("--no-subfolders", action="store_true", help="read clips from the video folder only, not its subfolders")
    ap.add_argument("--gyroflow", help="path to the Gyroflow executable")
    ap.add_argument("--ffmpeg", help="path to ffmpeg")
    ap.add_argument("--ffprobe", help="path to ffprobe")
    a = ap.parse_args(argv)

    try:
        t = pipeline.Tools.find(a.ffmpeg, a.ffprobe, a.gyroflow)
        problems = t.problems()
    except tools.ToolError as e:
        problems = [str(e)]
    if problems:
        print("\n".join(problems))
        return 2

    takes = pipeline.find_takes(a.gyro)
    clips = pipeline.find_clips(a.videos, subfolders=not a.no_subfolders)
    print(f"{len(takes)} gyro takes, {len(clips)} clips")

    done = already = no_gyro = failed = warned = 0
    for i, clip in enumerate(clips, 1):
        print(f"[{i}/{len(clips)}] {clip.path.name}", flush=True)
        try:
            state = pipeline.match(clip, takes, t, redo=a.force)
        except (tools.ToolError, OSError, KeyError, IndexError, ValueError) as e:
            print(f"FAIL {clip.path.name}: {e}")
            failed += 1
            continue
        if state == pipeline.DONE:
            print(f"skip {clip.path.name}: already done")
            already += 1
            continue
        if state == pipeline.NO_GYRO:
            print(f"skip {clip.path.name}: no gyro data")
            no_gyro += 1
            continue
        if clip.gcsv:
            print(f"{clip.path.name}: {clip.gcsv.name}, {clip.frames} frames at {clip.fps:g} fps")
        else:
            how = "timecode" if clip.method == "timecode" else f"correlation {clip.corr:.2f}"
            print(f"{clip.path.name}: {clip.take.path.name} from {clip.start_s:.2f} s ({how})")
        if a.dry_run:
            continue
        print("     syncing in Gyroflow…", flush=True)
        started = time.monotonic()
        try:
            offsets = pipeline.generate(clip, t)
        except (tools.ToolError, OSError) as e:
            print(f"FAIL {clip.path.name}: {e}")
            failed += 1
            continue
        spread = f"{offsets[-1] - offsets[0]:.1f}" if offsets else "n/a"
        print(f"ok   {clip.project.name}: sync offsets ms [{', '.join(f'{o:.1f}' for o in offsets)}], "
              f"spread {spread} ({time.monotonic() - started:.0f} s)")
        warning = pipeline.sync_warning(offsets, clip.method == "timecode")
        if warning:
            print(f"     check sync: {warning}")
            warned += 1
        done += 1

    print(f"\n{done} written ({warned} to check), {already} already done, {no_gyro} without gyro data, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
