"""Tkinter front end for the pipeline: two folders, a clip table, two buttons."""

from __future__ import annotations

import json
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from . import pipeline, tools

try:
    # Drag and drop needs the tkdnd extension; the app works without it.
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    TkinterDnD = None

CONFIG = Path.home() / ".gyroflow-batch-resolve.json"

# Errors a single clip can hit without stopping the run. Anything else is a bug
# and is reported the same way, so one bad file never ends a batch.
CLIP_ERRORS = (tools.ToolError, OSError, KeyError, IndexError, ValueError)


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG.write_text(json.dumps(cfg, indent=1))
    except OSError:
        pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.clips: list[pipeline.Clip] = []
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        # What the worker is on, for the status line: (index, total, clip, step, started).
        self.current: tuple | None = None

        root.title("Gyroflow Batch for Resolve")
        root.geometry("1000x680")
        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        self.gyro_dir = tk.StringVar(value=self.cfg.get("gyro_dir", ""))
        self.video_dir = tk.StringVar(value=self.cfg.get("video_dir", ""))
        self.gyroflow = tk.StringVar(value=self.cfg.get("gyroflow", ""))
        self.ffmpeg = tk.StringVar(value=self.cfg.get("ffmpeg", ""))
        self.redo = tk.BooleanVar(value=False)

        rows = [("Gyro source (SD card)", self.gyro_dir, True),
                ("Video source (recorder SSD)", self.video_dir, True),
                ("Gyroflow executable (blank = auto)", self.gyroflow, False),
                ("ffmpeg (blank = auto)", self.ffmpeg, False)]
        for i, (label, var, is_dir) in enumerate(rows):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", pady=2)
            entry = ttk.Entry(frm, textvariable=var)
            entry.grid(row=i, column=1, sticky="ew", padx=6)
            ttk.Button(frm, text="Choose…", command=lambda v=var, d=is_dir: self.choose(v, d)).grid(row=i, column=2)
            if TkinterDnD:
                entry.drop_target_register(DND_FILES)
                entry.dnd_bind("<<Drop>>", lambda e, v=var, d=is_dir: self.dropped(e, v, d))

        self.tool_status = ttk.Label(frm, text="")
        self.tool_status.grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))

        bar = ttk.Frame(frm)
        bar.grid(row=5, column=0, columnspan=3, sticky="w", pady=8)
        self.match_btn = ttk.Button(bar, text="1. Match clips", command=self.match)
        self.match_btn.pack(side="left")
        self.gen_btn = ttk.Button(bar, text="2. Write .gyroflow files", command=self.generate, state="disabled")
        self.gen_btn.pack(side="left", padx=6)
        self.cancel_btn = ttk.Button(bar, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left")
        ttk.Checkbutton(bar, text="Redo clips that are already done", variable=self.redo).pack(side="left", padx=12)

        cols = ("clip", "take", "start", "match", "status")
        self.table = ttk.Treeview(frm, columns=cols, show="headings", height=12)
        for c, title, w in zip(cols, ("Clip", "Gyro take", "Starts at", "Match", "Status"), (250, 110, 80, 60, 460)):
            self.table.heading(c, text=title)
            self.table.column(c, width=w, anchor="w")
        self.table.tag_configure("failed", foreground="#b00020")
        self.table.tag_configure("warn", foreground="#b36b00")
        self.table.tag_configure("skipped", foreground="#777777")
        self.table.grid(row=6, column=0, columnspan=3, sticky="nsew")
        frm.rowconfigure(6, weight=3)

        # Progress: the activity bar moves whenever work is running, so a stuck
        # UI is visible; the counter bar and timer show where the run is.
        prog = ttk.Frame(frm)
        prog.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        prog.columnconfigure(2, weight=1)
        self.activity = ttk.Progressbar(prog, mode="indeterminate", length=80)
        self.activity.grid(row=0, column=0)
        self.counter = ttk.Progressbar(prog, mode="determinate", length=200)
        self.counter.grid(row=0, column=1, padx=8)
        self.status = ttk.Label(prog, text="Idle")
        self.status.grid(row=0, column=2, sticky="w")

        self.log = tk.Text(frm, height=8, state="disabled")
        self.log.grid(row=8, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        frm.rowconfigure(8, weight=1)

        if not TkinterDnD:
            self.write("Drag and drop is off (tkinterdnd2 not installed); use Choose….")
        root.after(100, self.pump)
        self.check_tools()

    # --- UI helpers -------------------------------------------------------

    def choose(self, var: tk.StringVar, is_dir: bool) -> None:
        path = filedialog.askdirectory() if is_dir else filedialog.askopenfilename()
        if path:
            var.set(path)
            if not is_dir:
                self.check_tools()

    def dropped(self, event, var: tk.StringVar, is_dir: bool) -> None:
        paths = self.root.tk.splitlist(event.data)
        if not paths:
            return
        path = Path(paths[0])
        # A file dropped on a folder field means the folder it is in.
        if is_dir and not path.is_dir():
            path = path.parent
        var.set(str(path))
        if not is_dir:
            self.check_tools()

    def write(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def show(self, clip: pipeline.Clip) -> None:
        values = (clip.path.name,
                  clip.take.path.name if clip.take else "",
                  f"{clip.start_s:.2f} s" if clip.take else "",
                  f"{clip.corr:.2f}" if clip.take else "",
                  clip.status)
        tag = ("failed" if clip.status.startswith(("failed", "cancelled")) else
               "warn" if "check sync" in clip.status else
               "skipped" if clip.status.startswith(("skipped", "already")) else "")
        iid = str(clip.path)
        if self.table.exists(iid):
            self.table.item(iid, values=values, tags=(tag,))
        else:
            self.table.insert("", "end", iid=iid, values=values, tags=(tag,))
        self.table.see(iid)

    def pump(self) -> None:
        # Worker threads never touch Tk; they post here.
        while not self.events.empty():
            kind, arg = self.events.get()
            if kind == "log":
                self.write(arg)
            elif kind == "clip":
                self.show(arg)
            elif kind == "step":
                self.current = (*arg, time.monotonic())
                index, total = arg[0], arg[1]
                self.counter.configure(maximum=max(total, 1), value=index - 1)
            elif kind == "tools":
                ok, text = arg
                self.tool_status.configure(text=text, foreground="" if ok else "#b00020")
            elif kind == "done":
                self.finish(arg)
        if self.busy and self.current:
            index, total, name, step, started = self.current
            elapsed = int(time.monotonic() - started)
            self.status.configure(text=f"Clip {index}/{total} · {name} · {step} · {elapsed // 60}:{elapsed % 60:02d}")
        self.root.after(250, self.pump)

    def start(self, label: str, work) -> None:
        if self.busy:
            return
        self.busy = True
        self.current = None
        tools.reset_cancel()
        self.match_btn.configure(state="disabled")
        self.gen_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.activity.start(15)
        self.counter.configure(value=0)
        self.status.configure(text=label)
        self.cfg.update(gyro_dir=self.gyro_dir.get(), video_dir=self.video_dir.get(),
                        gyroflow=self.gyroflow.get(), ffmpeg=self.ffmpeg.get())
        save_config(self.cfg)

        def run():
            summary = ""
            try:
                summary = work()
            except tools.Cancelled:
                summary = "Cancelled."
            except Exception as e:   # shown to the user rather than lost in a thread
                summary = f"Stopped: {e}"
            self.events.put(("done", summary))
        threading.Thread(target=run, daemon=True).start()

    def finish(self, summary: str) -> None:
        self.busy = False
        self.current = None
        self.activity.stop()
        # Full only when the run got to the end; a cancelled or stopped run keeps its position.
        if not summary.startswith(("Cancelled", "Stopped")):
            self.counter.configure(value=self.counter.cget("maximum"))
        self.status.configure(text=summary or "Done.")
        if summary:
            self.write(summary)
        self.match_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        ready = any(c.status == "matched" for c in self.clips)
        self.gen_btn.configure(state="normal" if ready else "disabled")

    def cancel(self) -> None:
        self.status.configure(text="Cancelling…")
        tools.cancel()

    def find_tools(self) -> pipeline.Tools:
        ffmpeg = self.ffmpeg.get() or None
        ffprobe = str(Path(ffmpeg).with_name(Path(ffmpeg).name.replace("ffmpeg", "ffprobe"))) if ffmpeg else None
        return pipeline.Tools.find(ffmpeg, ffprobe, self.gyroflow.get() or None)

    def check_tools(self) -> None:
        """Find the tools off the UI thread and show what is wrong before a run."""
        def work():
            try:
                t = self.find_tools()
                problems = t.problems()
            except tools.ToolError as e:
                self.events.put(("tools", (False, f"✗ {e}")))
                return
            if problems:
                self.events.put(("tools", (False, "✗ " + " · ".join(problems))))
            else:
                self.events.put(("tools", (True, f"✓ Gyroflow {t.gyroflow} · ffmpeg {t.ffmpeg}")))
        threading.Thread(target=work, daemon=True).start()

    # --- actions ----------------------------------------------------------

    def match(self) -> None:
        gyro_dir, video_dir = Path(self.gyro_dir.get()), Path(self.video_dir.get())
        for name, d in (("Gyro source", gyro_dir), ("Video source", video_dir)):
            if not str(d) or str(d) == "." or not d.is_dir():
                self.write(f"{name} is not a folder that exists: {d}")
                return
        self.table.delete(*self.table.get_children())
        self.check_tools()
        redo = self.redo.get()

        def work():
            t = self.find_tools()
            post = lambda s: self.events.put(("log", s))
            takes = pipeline.find_takes(gyro_dir, post)
            self.clips = pipeline.find_clips(video_dir)
            post(f"{len(takes)} gyro takes, {len(self.clips)} clips")
            if not takes:
                post("No .GYR + .json pairs in the gyro source.")
            for clip in self.clips:
                self.events.put(("clip", clip))
            total = len(self.clips)
            for i, clip in enumerate(self.clips, 1):
                tools.check_cancel()
                step = lambda s, i=i, c=clip: self.events.put(("step", (i, total, c.path.name, s)))
                step("checking")
                try:
                    state = pipeline.match(clip, takes, t, redo=redo, step=step)
                except tools.Cancelled:
                    clip.status = "cancelled"
                    self.events.put(("clip", clip))
                    raise
                except Exception as e:
                    clip.take = None
                    clip.status = f"failed: {e}"
                else:
                    clip.status = {pipeline.DONE: "already done", pipeline.NO_GYRO: "skipped: no gyro data",
                                   pipeline.MATCHED: "matched"}[state]
                self.events.put(("clip", clip))
            n = {s: sum(c.status == s for c in self.clips) for s in ("matched", "already done")}
            failed = sum(c.status.startswith("failed") for c in self.clips)
            return (f"Matched: {n['matched']} to write, {n['already done']} already done"
                    + (f", {failed} failed" if failed else "") + ".")
        self.start("Matching…", work)

    def generate(self) -> None:
        def work():
            t = self.find_tools()
            todo = [c for c in self.clips if c.take is not None and c.status == "matched"]
            written = warned = failed = 0
            for i, clip in enumerate(todo, 1):
                tools.check_cancel()
                self.events.put(("step", (i, len(todo), clip.path.name, "syncing in Gyroflow")))
                clip.status = "syncing in Gyroflow…"
                self.events.put(("clip", clip))
                try:
                    offsets = pipeline.generate(clip, t)
                except tools.Cancelled:
                    clip.status = "cancelled"
                    self.events.put(("clip", clip))
                    raise
                except Exception as e:
                    clip.status = f"failed: {e}"
                    failed += 1
                else:
                    spread = f"{offsets[-1] - offsets[0]:.1f} ms" if offsets else "n/a"
                    clip.status = f"written, {len(offsets)} sync points, spread {spread}"
                    warning = pipeline.sync_warning(offsets)
                    if warning:
                        clip.status = f"written, check sync: {warning}"
                        warned += 1
                    written += 1
                self.events.put(("clip", clip))
            return (f"{written} .gyroflow files written next to their clips"
                    + (f", {warned} to check" if warned else "") + (f", {failed} failed" if failed else "") + ".")
        self.start("Writing…", work)


def main() -> None:
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
