"""
Rerun a script whenever a file it depends on changes.

The script runs in this process, so build123d and OCP stay imported
between runs (importing them takes seconds). Before each run the project's
own modules are dropped from sys.modules, so edited helpers are imported
afresh; installed libraries are kept. Most parts are then cache hits and
the rerun takes about as long as the changed parts take to build.

fresh=True runs the script in a new Python process each time instead, for
scripts that don't tolerate being rerun in one process.
"""
import os
import runpy
import subprocess
import sys
import time
import traceback
import warnings
from pathlib import Path

from . import core, keys

PATTERNS = {".py", ".yaml", ".yml", ".json", ".toml", ".csv", ".txt",
            ".dxf", ".svg", ".step", ".stp", ".stl"}
SKIP_DIRS = {"__pycache__", "node_modules", "venv", "build", "dist"}


def project_files(root):
    """Files under root a script may depend on."""
    out = set()
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs
                   if not x.startswith(".") and x not in SKIP_DIRS
                   and not x.endswith(".egg-info")]
        for f in files:
            if os.path.splitext(f)[1].lower() in PATTERNS:
                out.add(os.path.join(d, f))
    return out


def _local_modules(root):
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if not f or keys.is_library_path(f):
            continue
        try:
            Path(f).resolve().relative_to(root)
        except ValueError:
            continue
        yield name, f


def snapshot(paths):
    out = {}
    for p in paths:
        try:
            st = os.stat(p)
            out[p] = (st.st_mtime_ns, st.st_size)
        except OSError:
            out[p] = None
    return out


def changed(before):
    """Paths whose mtime or size differ from the snapshot."""
    now = snapshot(before)
    return sorted(p for p in before if now[p] != before[p])


def run_once(script, argv=(), root=None):
    """Run script as __main__ in this process. Returns True if it finished
    without an exception."""
    script = Path(script).resolve()
    root = Path(root).resolve() if root else script.parent
    for name, _ in list(_local_modules(root)):
        del sys.modules[name]
    core.reset()
    old_argv, old_path = sys.argv, list(sys.path)
    # A .pyc records whole-second mtimes, so a quick edit that keeps the
    # file's size could load the old bytecode. Don't write any while
    # rerunning.
    old_bytecode, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    sys.argv = [str(script), *argv]
    sys.path.insert(0, str(script.parent))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("always", keys.CacheWarning)
            runpy.run_path(str(script), run_name="__main__")
        return True
    except SystemExit as e:
        return e.code in (None, 0)
    except Exception:
        traceback.print_exc()
        return False
    finally:
        sys.argv = old_argv
        sys.path[:] = old_path
        sys.dont_write_bytecode = old_bytecode


def watch(script, argv=(), root=None, fresh=False, interval=0.3,
          out=print, runs=None):
    """Run script, then again after every change, until Ctrl+C (or `runs`
    runs, for tests)."""
    script = Path(script).resolve()
    root = Path(root).resolve() if root else script.parent
    n = 0
    while runs is None or n < runs:
        n += 1
        t0 = time.perf_counter()
        if fresh:
            r = subprocess.run([sys.executable, str(script), *argv])
            ok = r.returncode == 0
            deps = set()
        else:
            ok = run_once(script, argv, root)
            deps = {f for _, f in _local_modules(root)} | core.run_files()
        dt = time.perf_counter() - t0
        out("cadcache: %s in %.2fs%s" % (
            "ran" if ok else "FAILED", dt,
            "" if fresh else " - " + core.summary()))
        if runs is not None and n >= runs:
            break
        before = snapshot(project_files(root) | deps | {str(script)})
        out("cadcache: watching %d files under %s" % (len(before), root))
        try:
            while True:
                time.sleep(interval)
                hits = changed(before)
                if hits:
                    time.sleep(0.1)          # let editors finish saving
                    out("cadcache: changed %s" % ", ".join(
                        os.path.relpath(p, root) for p in hits[:5]))
                    break
        except KeyboardInterrupt:
            return
