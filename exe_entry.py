#!/usr/bin/env python3
"""
exe_entry.py  –  Single entry-point for the PyInstaller one-file bundle.

When the .exe is launched **without** ``--run-script`` it starts the
Testbench GUI (gui_launcher.py).

When launched with ``--run-script <path> [args …]`` it executes the
given Python script via ``runpy.run_path`` in the current process,
forwarding all remaining arguments.  This allows the GUI to spawn
child scripts as ``subprocess.Popen([sys.executable, "--run-script",
"messablauf.py", ...])`` even when ``sys.executable`` is the frozen
.exe itself.
"""

import os
import sys
import runpy
from pathlib import Path


def _app_dir() -> Path:
    """Directory containing the .exe (or this script when not frozen)."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _bundle_dir() -> Path:
    """Directory where PyInstaller extracted bundled files (data/scripts).
    In one-file mode this is a temp folder (_MEIPASS); unfrozen it's the
    script directory."""
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def main():
    # ── Dispatcher: --run-script <script.py> [args …] ────────────────
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-script":
        script = sys.argv[2]
        # Resolve relative paths: look in bundle dir first, then app dir
        script_path = Path(script)
        if not script_path.is_absolute():
            # Bundled scripts live in _MEIPASS (one-file) or next to exe
            candidate = _bundle_dir() / script_path
            if candidate.exists():
                script_path = candidate
            else:
                script_path = _app_dir() / script_path

        # Remove our dispatcher args so the child script sees only its own
        sys.argv = [str(script_path)] + sys.argv[3:]

        # Make sure both dirs are in sys.path so local imports work
        for d in (_bundle_dir(), _app_dir()):
            ds = str(d)
            if ds not in sys.path:
                sys.path.insert(0, ds)

        # Change working directory to app dir (data files live there)
        os.chdir(_app_dir())

        import traceback
        try:
            runpy.run_path(str(script_path), run_name="__main__")
        except Exception:
            traceback.print_exc()
            sys.exit(1)
        return

    # ── Default: launch the GUI ──────────────────────────────────────
    # Ensure CWD is the app directory
    os.chdir(_app_dir())
    for d in (_bundle_dir(), _app_dir()):
        ds = str(d)
        if ds not in sys.path:
            sys.path.insert(0, ds)

    import gui_launcher
    import tkinter as tk
    root = tk.Tk()
    gui_launcher.TestBenchLauncher(root)
    root.mainloop()


if __name__ == "__main__":
    main()
