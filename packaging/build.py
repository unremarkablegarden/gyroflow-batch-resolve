"""Build the GUI app and the CLI binary for the current platform into dist/.

PyInstaller cannot cross-compile, so each platform builds its own; the GitHub
workflow runs this on macOS, Windows and Linux.
"""

from pathlib import Path

import PyInstaller.__main__

HERE = Path(__file__).resolve().parent
# --paths: an editable install is an import hook PyInstaller does not follow.
COMMON = ["--noconfirm", "--clean", "--paths", str(HERE.parent)]

# tkinterdnd2 ships the tkdnd binaries as package data.
PyInstaller.__main__.run([str(HERE / "gui_entry.py"), "--name", "Gyroflow Batch Resolve", "--windowed",
                          "--collect-all", "tkinterdnd2", *COMMON])
PyInstaller.__main__.run([str(HERE / "cli_entry.py"), "--name", "gyroflow-batch", "--onefile", "--console", *COMMON])
