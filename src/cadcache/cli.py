"""cadcache command line: watch, run, ls, clear."""
import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from . import core, watch


def _entries(cache_dir):
    for side in sorted(Path(cache_dir).glob("*/*.json")):
        try:
            meta = json.loads(side.read_text(encoding="utf-8"))
        except ValueError:
            continue
        yield side, meta


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="cadcache", description="Per-part cache for build123d.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, text in (("watch", "run a script, and again on every change"),
                       ("run", "run a script once and report the parts")):
        p = sub.add_parser(name, help=text)
        p.add_argument("script")
        p.add_argument("args", nargs=argparse.REMAINDER,
                       help="arguments for the script")
        p.add_argument("--root", help="project folder to watch and reload "
                       "(default: the script's folder)")
        if name == "watch":
            p.add_argument("--fresh", action="store_true",
                           help="new Python process for every run")
        p.add_argument("-v", "--verbose", action="store_true",
                       help="log cache hits too")

    for name, text in (("ls", "list cache entries"),
                       ("clear", "delete the cache folder")):
        p = sub.add_parser(name, help=text)
        p.add_argument("cache_dir", nargs="?", default=".cadcache")

    a = ap.parse_args(argv)
    if a.cmd in ("watch", "run"):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        log = logging.getLogger("cadcache")
        log.addHandler(handler)
        log.setLevel(logging.DEBUG if a.verbose else logging.INFO)
        if a.root:
            core.configure(root=a.root)
        if a.cmd == "run":
            ok = watch.run_once(a.script, a.args, a.root)
            print("cadcache: " + core.summary())
            return 0 if ok else 1
        watch.watch(a.script, a.args, a.root, fresh=a.fresh)
        return 0
    if a.cmd == "ls":
        for side, meta in _entries(a.cache_dir):
            args = ", ".join("%s=%s" % kv
                             for kv in meta.get("arguments", {}).items())
            print("%-40s %6.2fs  %s(%s)" % (
                side.with_suffix(".brep").relative_to(a.cache_dir),
                meta.get("built_s", 0), meta.get("function", "?"), args))
        return 0
    if a.cmd == "clear":
        d = Path(a.cache_dir)
        if d.is_dir():
            if not any(d.glob("*/*.json")) and any(d.iterdir()):
                print("%s doesn't look like a cadcache folder" % d)
                return 1
            shutil.rmtree(d)
            print("removed %s" % d)
        return 0


if __name__ == "__main__":
    sys.exit(main())
