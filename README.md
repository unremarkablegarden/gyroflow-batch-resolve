# gyroflow-batch-resolve

Stabilise a whole card of Sigma fp + Atomos Ninja V ProRes RAW clips in DaVinci Resolve with Gyroflow, without setting up each clip by hand.

![Gyroflow Batch for Resolve: two clips matched to their gyro takes, one written with 5 sync points 2.6 ms apart, the second syncing](docs/screenshot.webp)

The tool matches every recorder clip to the gyro log the camera wrote, corrects the camera's lens profile for the clip, runs Gyroflow's autosync, and writes `<clip>.gyroflow` next to each clip. The Gyroflow OFX plugin in Resolve picks those files up by name. No video is rendered.

The `.gyroflow` files are ordinary Gyroflow projects with the gyro data embedded, so they work in any editor with a Gyroflow plugin: DaVinci Resolve, Final Cut Pro, Adobe Premiere Pro and After Effects, and other OpenFX hosts. They also open in the Gyroflow app itself for rendering there. The steps below are for Resolve; in other editors, load `<clip>.gyroflow` into the plugin as that editor's plugin documentation describes.

## What you need

- A Sigma fp (firmware 5.02) running the fpSup Base build with the HDMI record hooks. It writes `H001_577.GYR` + `H001_577.json` to the SD card root for every take recorded to the Ninja.
- [Gyroflow](https://github.com/gyroflow/gyroflow/releases) 1.6.3 or newer, the GitHub build. The Mac App Store build is sandboxed and its command line cannot read clips.
- The Gyroflow OFX plugin for Resolve (installed from Gyroflow: Video editor plugins → OpenFX).
- ffmpeg and ffprobe, version 8.0 or newer (first with a ProRes RAW decoder). macOS: `brew install ffmpeg`.

## Shooting

- Start and stop recording with the REC button on the fp body. Stopping from the Ninja's screen does not reach the camera, so the gyro log keeps running; the tool still finds the clip inside that log, but one log then spans several clips.
- Clips need some camera motion for matching and autosync to lock. A locked-off tripod shot has nothing to stabilise anyway.

## Use

### App

Run **Gyroflow Batch Resolve**.

1. Gyro source: the SD card (or a copy of its `.GYR` and `.json` files).
2. Video source: the Ninja SSD (or the folder you copied the clips to). Searched recursively.
3. **1. Match clips**: reads each clip and finds its place in the gyro logs. The Match column is a correlation from 0 to 1; below 0.5 the clip is left out.
4. **2. Write .gyroflow files**: runs Gyroflow for each matched clip. The Status column shows the sync points and their spread. A spread of a few ms is a good sync; hundreds of ms means it did not lock.

Re-running on the same card only does the new clips: a clip whose `.gyroflow` already holds sync points is marked "already done" without being read (tick "Redo clips that are already done" to redo them). A `.gyroflow` without sync points, left by an interrupted run, is redone. Clips with no gyro data (longer than every log, or matching none) are skipped, not failed.

Folders can be dragged onto the path fields. Folder and tool paths are remembered in `~/.gyroflow-batch-resolve.json`.

While it works, the small bar at the bottom keeps moving and the status line shows the clip, the step and a running timer, so a stalled run is visible. Cancel stops the running ffmpeg or Gyroflow process; clips that were not reached keep their state and can be written in a later run.

What can go wrong, and what it looks like:

| Problem | Shown as |
|---|---|
| Gyroflow or ffmpeg missing, the App Store Gyroflow, or an ffmpeg without ProRes RAW | ✗ line under the path fields, at startup |
| A clip cannot be read (unsupported file, drive ejected) | that clip `failed: …` in red; the run continues |
| Gyroflow fails on a clip | `failed: …` with the path of its full output under `~/.gyroflow-batch-resolve/logs/` |
| Gyroflow hangs | stopped after 15 min, `failed: … did not finish` |
| Autosync did not lock (fewer than 2 sync points, or spread over 50 ms) | `written, check sync: …` in orange; open that clip in Gyroflow and sync by hand |

### Command line

```bash
gyroflow-batch /Volumes/NINJA --gyro /Volumes/SigmaFP --dry-run
gyroflow-batch /Volumes/NINJA --gyro /Volumes/SigmaFP
```

Same skipping rules as the app. `--force` redoes clips that are already done. `--gyroflow`, `--ffmpeg` and `--ffprobe` set tool paths when they are not found on their own.

## In DaVinci Resolve

The plugin reads the file path of the clip it sits on and loads `<clip name>.gyroflow` from the same folder. Every copy of the effect does this for its own clip, so the effect is set up once and pasted onto the rest.

1. Import the clips from where the `.gyroflow` files were written, and put them on a timeline. Do not move or rename the clips or the `.gyroflow` files afterwards.
2. Edit page → Effects → search "Gyroflow" → drag the effect onto the first clip. Check that it stabilises: the Inspector's Gyroflow panel shows the project file with that clip's name.
3. Select that clip, copy it (⌘C / Ctrl+C).
4. Select all the other clips → Paste Attributes (⌥⌘V / Alt+Ctrl+V) → tick **Plugins** → Apply.
5. Spot-check a few clips: each should show its own `.gyroflow` in the Inspector.

If a clip does not pick up its file: in the Inspector's Gyroflow panel, press **Load for current file** (Resolve Studio only), or use the Browse button next to Project file and pick `<clip name>.gyroflow`.

Compound clips do not expose a file path to the plugin, so apply the effect before making compound clips, or inside them.

Stabilisation settings (smoothness, zoom, horizon lock) can be changed per clip in the Inspector after this, as usual.

## How it works

- `.GYR` → `.gcsv`: a port of the fpSup web converter, byte-identical output, accelerometer included for horizon lock.
- Matching: each clip is decoded at 96×54 greyscale; its frame-to-frame change is cross-correlated with the gyro's angular rate over every position in every log. The best position is where the clip starts in the log.
- Lens profile: the camera writes the profile of the mode it monitors in over HDMI (3856×2170 @59.94). The tool sets the clip's dimensions and frame rate and moves the principal point to the centre. The rolling-shutter readout (`frame_readout_time`) is kept as the camera wrote it; it is not yet verified for HDMI RAW.
- Gyroflow 1.6.3 reads autosync settings from the lens profile, not from the command line's `-s`, so they are written into the profile. The `.gyroflow` is exported with the gyro data embedded, so it needs no other file.

## Build

```bash
uv venv --python 3.12 --managed-python .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/python packaging/build.py   # dist/: the app and the gyroflow-batch binary
```

PyInstaller builds for the platform it runs on. `.github/workflows/build.yml` builds macOS, Windows and Linux on a `v*` tag or by hand from the Actions tab.
