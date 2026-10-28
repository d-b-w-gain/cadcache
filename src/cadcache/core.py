"""
@part and @assembly.

A @part function is called as usual and returns a build123d shape. The
first call with given inputs builds it and stores it; later calls with the
same inputs load it from the cache instead. Either way the caller gets a
fresh build123d object loaded from the stored BREP, so a part behaves the
same whether it was just built or not.

A part is rebuilt when its key changes (arguments, its code or a helper's,
globals it reads, the build123d version - see keys.py) or when a file it
read while building has changed since.
"""
import builtins
import functools
import inspect
import io
import json
import logging
import os
import sys
import threading
import time
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePath

from . import keys, partnums, store
from .keys import FileReadWarning, GlobalReadWarning, CacheWarning

log = logging.getLogger("cadcache")


# ------------------------------------------------------------- settings
class _Settings:
    enabled = os.environ.get("CADCACHE", "").lower() not in (
        "0", "off", "false", "no")
    cache_dir = os.environ.get("CADCACHE_DIR") or None
    root = None
    salt = None
    before_save = None
    attributes = ()
    on_hit = None
    validate = None
    rebind = None
    defer_saves = False


settings = _Settings()

# Deferred saves (configure(defer_saves=True)): entry paths -> (snapshot,
# built shape, sidecar path), written by flush().
_pending = {}


def configure(*, cache_dir=None, root=None, enabled=None, salt=None,
              before_save=None, attributes=None, on_hit=None,
              validate=None, rebind=None, defer_saves=None):
    """cache_dir: where entries go (default: CADCACHE_DIR, else .cadcache
    in the project folder of each part's module). root: the project
    folder - code under it is hashed into keys, code elsewhere (installed
    libraries) isn't. enabled: the global switch (also CADCACHE=off).

    salt: any keyable value, mixed into every key - for things outside
    the code that change what a part's entry should hold (an app's
    version, export settings). before_save: fn(shape), called on a newly
    built shape just before it is stored, e.g. to tessellate it so the
    BREP carries the mesh. attributes: names of plain-data attributes to
    keep on a shape and its children (BREP drops Python attributes).

    on_hit: fn(shape, effects), called when a part loads from the cache
    with the effects its build recorded (see record_effect), so the app
    can redo them on the loaded shape; returning False (having changed
    nothing) builds the part instead. validate: fn(shape, effects),
    called on a new build before it is stored; returning a reason string
    returns the part as built but doesn't store it. rebind: fn(built,
    loaded, effects), called after a new build is stored: the caller gets
    `loaded` (read back from the entry), not `built`, so anything the
    effects attached to `built` can be moved across.

    defer_saves: a new build returns the shape it built, and its entry is
    snapshotted (cheap) and written by flush() later - after a result has
    been delivered, say. A repeat call before then is served from the
    snapshot. rebind isn't called: the caller has the built shape."""
    if cache_dir is not None:
        settings.cache_dir = str(cache_dir)
    if root is not None:
        settings.root = Path(root).resolve()
    if enabled is not None:
        settings.enabled = bool(enabled)
    if salt is not None:
        keys.canon(salt, "salt")
        settings.salt = salt
    if before_save is not None:
        settings.before_save = before_save
    if attributes is not None:
        settings.attributes = tuple(attributes)
    if on_hit is not None:
        settings.on_hit = on_hit
    if validate is not None:
        settings.validate = validate
    if rebind is not None:
        settings.rebind = rebind
    if defer_saves is not None:
        settings.defer_saves = bool(defer_saves)


def flush():
    """Write the entries of deferred saves. A failed write is logged and
    skipped (the part just builds again next time). Returns how many were
    written."""
    written = 0
    pending = list(_pending.items())
    _pending.clear()
    with _paused():
        for (brep, side), (snap, built) in pending:
            try:
                if settings.before_save is not None:
                    settings.before_save(built)
                store.write(snap, brep, side)
                written += 1
            except Exception as e:
                log.warning("couldn't write cache entry %s (%s)", brep, e)
    return written


def pending_saves():
    """How many deferred saves are waiting for flush()."""
    return len(_pending)


@contextmanager
def disabled():
    """Build everything inside this block, without reading or writing the
    cache."""
    old = settings.enabled
    settings.enabled = False
    try:
        yield
    finally:
        settings.enabled = old


def _root(fn):
    if settings.root is not None:
        return settings.root
    return keys.project_root(fn)


def _cache_dir(fn):
    if settings.cache_dir:
        return Path(settings.cache_dir)
    root = _root(fn)
    return (root or Path.cwd()) / ".cadcache"


# ------------------------------------------------------------ run stats
class Event:
    """One part call (or assembly). kind is "built", "cached",
    "uncached" (switched off), "uncacheable" (see not_cacheable) or
    "assembly". entry is the stored BREP's path for built and cached
    parts; reason says why a part was uncacheable."""
    __slots__ = ("name", "kind", "seconds", "key", "part_number", "entry",
                 "reason")

    def __init__(self, name, kind, seconds, key=None, part_number=None,
                 entry=None, reason=None):
        self.name, self.kind, self.seconds, self.key = \
            name, kind, seconds, key
        self.part_number, self.entry, self.reason = \
            part_number, entry, reason

    def __repr__(self):
        return "<%s %s %.3fs>" % (self.kind, self.name, self.seconds)


_events = []
_warned = set()


def events():
    """What happened since reset(), as Events."""
    return list(_events)


def reset():
    """Start a new run: clears events() and lets warnings show again."""
    _events.clear()
    _warned.clear()
    _run_files.clear()


def summary():
    built = [e for e in _events if e.kind == "built"]
    cached = [e for e in _events if e.kind == "cached"]
    off = [e for e in _events if e.kind in ("uncached", "uncacheable")]
    text = "%d parts: %d cached, %d built" % (
        len(built) + len(cached) + len(off), len(cached), len(built))
    if off:
        text += ", %d uncached" % len(off)
    if built:
        text += " (%s)" % ", ".join("%s %.2fs" % (e.name, e.seconds)
                                    for e in built)
    return text


def _warn(category, fn_name, what, message):
    if (fn_name, what) in _warned:
        return
    _warned.add((fn_name, what))
    warnings.warn(message, category, stacklevel=_caller_level())


def _caller_level():
    """stacklevel pointing at the first frame outside cadcache."""
    here = os.path.dirname(__file__)
    f, level = sys._getframe(1), 1
    while f is not None and os.path.dirname(f.f_code.co_filename) == here:
        f, level = f.f_back, level + 1
    return level


# ------------------------------------------------------- file recording
_local = threading.local()
_run_files = set()
_SKIP_SUFFIXES = {".py", ".pyc", ".pyd", ".so", ".dll", ".pth", ".brep"}


class _Frame(set):
    """One build in progress: the files it read, the effects it recorded,
    and why it can't be cached, if something said so."""
    reason = None

    def __init__(self):
        super().__init__()
        self.effects = []


def _frames():
    f = getattr(_local, "frames", None)
    if f is None:
        f = _local.frames = []
    return f


def not_cacheable(reason):
    """Called from inside a part's build: this build has an effect a
    cache hit wouldn't repeat (registering itself somewhere, say). The
    part returns what it built without storing it, and so does every part
    whose build is running around it. Outside a build it does nothing."""
    for frame in _frames():
        if frame.reason is None:
            frame.reason = str(reason)


def record_effect(data):
    """Called from inside a part's build: this build did something outside
    its shape (registered a component, say) that a cache hit must redo.
    data is plain JSON data; it is stored with the entry of this part and
    of every part whose build is running around it, and handed back to
    configure(on_hit=...) when one of them loads from the cache. A part
    that loads from the cache inside another build gets its effects back
    through on_hit, so the app records them again for the outer build.
    Outside a build it does nothing."""
    frames = _frames()
    if not frames:
        return
    data = json.loads(json.dumps(data, allow_nan=False))
    for frame in frames:
        frame.effects.append(data)


def _record(path, mode, flags):
    """Note a file opened for reading in every build running on this
    thread."""
    frames = getattr(_local, "frames", None)
    if not frames:
        return
    try:
        if path is None or isinstance(path, int):
            return
        if mode is not None:
            if any(c in mode for c in "wax+"):
                return
        elif flags & (os.O_WRONLY | os.O_RDWR):
            return
        p = os.fsdecode(os.fspath(path))
        if os.path.splitext(p)[1].lower() in _SKIP_SUFFIXES:
            return
        p = os.path.abspath(p)
        if keys.is_library_path(p):
            return
        for frame in frames:
            frame.add(p)
    except Exception:
        pass


@contextmanager
def _paused():
    """Don't record files cadcache itself reads (sources, sidecars)."""
    saved = _frames()
    _local.frames = []
    try:
        yield
    finally:
        _local.frames = saved


# Files are seen by wrapping open() only while a build runs, not by a
# sys.addaudithook: an audit hook can never be removed, and every audit
# event in the process (millions of attribute reads under a profiler or
# tracer) would then call into Python. Opens through a reference taken
# before the build started, or through _io.FileIO directly, aren't seen.
_watch_lock = threading.Lock()
_watch_depth = 0
_saved_opens = None


def _recording_open(real):
    @functools.wraps(real)
    def open(file, *args, **kwargs):
        if getattr(_local, "frames", None):
            mode = args[0] if args else kwargs.get("mode", "r")
            _record(file, mode, None)
        return real(file, *args, **kwargs)
    return open


def _recording_os_open(real):
    @functools.wraps(real)
    def open(path, *args, **kwargs):
        if getattr(_local, "frames", None):
            flags = args[0] if args else kwargs.get("flags", 0)
            _record(path, None, flags)
        return real(path, *args, **kwargs)
    return open


@contextmanager
def _watching():
    """Record files opened while a build runs. Nested and concurrent
    builds share one set of wrappers; the last one out restores open()."""
    global _watch_depth, _saved_opens
    with _watch_lock:
        if _watch_depth == 0:
            real = (builtins.open, io.open, os.open)
            ours_b = _recording_open(real[0])
            # io.open is normally builtins.open itself: wrap it once.
            ours_io = ours_b if real[1] is real[0] else _recording_open(real[1])
            ours = (ours_b, ours_io, _recording_os_open(real[2]))
            builtins.open, io.open, os.open = ours
            _saved_opens = (real, ours)
        _watch_depth += 1
    try:
        yield
    finally:
        with _watch_lock:
            _watch_depth -= 1
            if _watch_depth == 0:
                (real_b, real_io, real_os), (ours_b, ours_io, ours_os) = \
                    _saved_opens
                # Leave alone anything someone else swapped in meanwhile.
                if builtins.open is ours_b:
                    builtins.open = real_b
                if io.open is ours_io:
                    io.open = real_io
                if os.open is ours_os:
                    os.open = real_os
                _saved_opens = None


def _file_record(paths, cache_dir):
    cache = os.path.normcase(os.path.abspath(cache_dir)) + os.sep
    out = []
    for p in sorted(paths):
        if os.path.normcase(p).startswith(cache) or not os.path.isfile(p):
            continue
        st = os.stat(p)
        out.append({"path": p, "sha256": keys.file_sha(p),
                    "mtime_ns": st.st_mtime_ns, "size": st.st_size})
    return out


def _files_unchanged(files):
    for f in files:
        try:
            st = os.stat(f["path"])
        except OSError:
            return False
        if st.st_mtime_ns == f["mtime_ns"] and st.st_size == f["size"]:
            continue
        if st.st_size != f["size"] or keys.file_sha(f["path"]) != \
                f["sha256"]:
            return False
    return True


def run_files():
    """Files parts read while building, this run (for file watchers)."""
    return set(_run_files)


# ------------------------------------------------------------ decorators
def _name(fn):
    return fn.__qualname__.replace("<locals>.", "")


def _short(v, n=80):
    r = repr(v)
    return r if len(r) <= n else r[:n - 3] + "..."


class Part:
    """A cached part function. Call it like the function."""

    def __init__(self, fn, version=None, part_class=None):
        functools.update_wrapper(self, fn)
        self.fn = fn
        self.version = version
        self.part_class = part_class or fn.__name__.replace("_", "-")
        self.signature = inspect.signature(fn)
        self.__cadcache__ = "part"

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return functools.partial(self, obj)

    def _bind(self, args, kwargs):
        bound = self.signature.bind(*args, **kwargs)
        bound.apply_defaults()
        return bound.arguments

    def _report(self, kind, what, where):
        me = _name(self.fn)
        if kind == "global":
            _warn(GlobalReadWarning, me, what,
                  "part %s reads global %s (in %s). Its value is in the "
                  "cache key, but passing it as an argument keeps the part "
                  "self-contained." % (me, what, where))
        elif kind == "untracked":
            _warn(GlobalReadWarning, me, what,
                  "part %s reads global %s (in %s), which cadcache can't "
                  "key: changes to it won't rebuild the part. Pass it as "
                  "an argument." % (me, what, where))
        elif kind == "occt" and not self._has_path_args:
            _warn(FileReadWarning, me, what,
                  "part %s calls %s (in %s). Files OCCT reads aren't seen "
                  "by cadcache; pass the file as a pathlib.Path argument "
                  "so its content is in the key." % (me, what, where))

    def key(self, *args, **kwargs):
        """The cache key this call would use."""
        return self._key(self._bind(args, kwargs))[0]

    def part_number(self, *args, **kwargs):
        """The part number this call gets: AAA-XXXX-XXXXX, hashed from
        its inputs only, so a refactor doesn't change it (partnums.py)."""
        return self._key(self._bind(args, kwargs))[1]

    def _key(self, arguments):
        """(cache key, part number) for bound arguments."""
        self._has_path_args = any(isinstance(v, PurePath)
                                  for v in arguments.values())
        global_values = {}
        code, _ = keys.code_hash(self.fn, _root(self.fn), self._report,
                                 global_values)
        args = {k: keys.canon(v, "argument %r of %s" % (k, _name(self.fn)))
                for k, v in arguments.items()}
        key = keys.digest({
            "cadcache": keys.FORMAT, "runtime": _runtime(),
            "fn": "%s.%s" % (self.fn.__module__, self.fn.__qualname__),
            "code": code, "args": args, "version": self.version,
            "salt": keys.canon(settings.salt, "salt")})
        number = partnums.part_number(self.part_class, arguments,
                                      global_values)
        return key, number

    def paths(self, *args, **kwargs):
        """(brep, sidecar) paths for this call's cache entry."""
        key = self.key(*args, **kwargs)
        return store.entry_paths(_cache_dir(self.fn), _name(self.fn), key)

    def uncached(self, *args, **kwargs):
        """Build without the cache: neither read nor written."""
        return self.fn(*args, **kwargs)

    def _load(self, key, brep, side):
        pending = _pending.get((brep, side))
        if pending is not None:          # built earlier this run, not written
            snap = pending[0]
            return store.from_snapshot(snap), snap[1]
        meta = store.read_sidecar(side)
        if not (meta and meta.get("key") == key and brep.exists() and
                meta.get("sidecar") == store.SIDECAR_VERSION and
                _files_unchanged(meta.get("files", ()))):
            return None
        try:
            return store.load(brep, meta), meta
        except Exception as e:          # damaged entry: build it again
            log.warning("cache entry %s unreadable (%s), rebuilding",
                        brep, e)
            return None

    def __call__(self, *args, **kwargs):
        name = _name(self.fn)
        if not settings.enabled:
            t0 = time.perf_counter()
            result = self.fn(*args, **kwargs)
            _events.append(Event(name, "uncached", time.perf_counter() - t0))
            return result
        arguments = self._bind(args, kwargs)
        t0 = time.perf_counter()
        with _paused():
            key, number = self._key(arguments)
            cache_dir = _cache_dir(self.fn)
            brep, side = store.entry_paths(cache_dir, name, key)
            hit = self._load(key, brep, side)
        if hit is not None:
            shape, meta = hit
            # Whoever called this part depends on the files it read too.
            read = [f["path"] for f in meta.get("files", ())]
            for frame in _frames():
                frame.update(read)
            _run_files.update(read)
            store.mark(shape, key, number)
            effects = meta.get("effects") or []
            if effects and settings.on_hit is not None and \
                    settings.on_hit(shape, effects) is False:
                # The app can't redo these effects now (something they
                # refer to is missing): build the part instead.
                log.info("cache entry for %s can't be replayed, rebuilding",
                         name)
                hit = None
        if hit is not None:
            dt = time.perf_counter() - t0
            _events.append(Event(name, "cached", dt, key, number, str(brep)))
            log.debug("cached %s (%.3fs)", name, dt)
            return shape

        t0 = time.perf_counter()
        frame = _Frame()
        _frames().append(frame)
        try:
            with _watching():
                result = self.fn(*args, **kwargs)
        finally:
            _frames().pop()
        built_s = time.perf_counter() - t0
        from build123d import Shape
        if not isinstance(result, Shape):
            raise TypeError(
                "part %s returned %s, not a build123d shape (in builder "
                "mode, return builder.part / .sketch / .line)"
                % (name, type(result).__name__))
        reason = frame.reason
        if reason is not None:
            # An outer build includes this one's effect, so it can't be
            # stored either (not_cacheable already marked it).
            for outer in _frames():
                outer.update(frame)
        elif settings.validate is not None:
            # Only this entry is refused: an outer build may still be
            # storable (it may have made what this one only borrowed).
            reason = settings.validate(result, list(frame.effects))
        if reason is not None:
            result._cadcache_pn = number
            result._cadcache_loc = keys.location_values(result.location)
            _events.append(Event(name, "uncacheable", built_s, key, number,
                                 reason=str(reason)))
            log.info("built %s (%.2fs), not cached: %s", name, built_s,
                     reason)
            return result

        with _paused():
            files = _file_record(frame, cache_dir)
        _run_files.update(f["path"] for f in files)
        arg_files = {os.path.normcase(os.path.abspath(v))
                     for v in arguments.values()
                     if isinstance(v, (str, PurePath)) and v}
        for f in files:
            if os.path.normcase(f["path"]) not in arg_files:
                _warn(FileReadWarning, name, f["path"],
                      "part %s read %s, which isn't an argument. It is "
                      "tracked (a change rebuilds the part), but passing "
                      "it as a pathlib.Path argument puts it in the key."
                      % (name, f["path"]))
        meta = {
            "key": key,
            "function": "%s.%s" % (self.fn.__module__, self.fn.__qualname__),
            "part_number": number,
            "arguments": {k: _short(v) for k, v in arguments.items()},
            "built_s": round(built_s, 3),
            "created": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "files": files,
            "effects": frame.effects,
        }
        if settings.defer_saves:
            # The caller gets what was built; the entry is fixed as it is
            # now and written by flush().
            with _paused():
                _pending[(brep, side)] = (
                    store.snapshot(result, meta, settings.attributes), result)
            store.mark(result, key, number)
            _events.append(Event(name, "built", built_s, key, number,
                                 str(brep)))
            log.info("built %s (%.2fs), entry deferred", name, built_s)
            return result
        with _paused():
            if settings.before_save is not None:
                settings.before_save(result)
            store.save(result, meta, brep, side, settings.attributes)
            shape = store.load(brep, store.read_sidecar(side))
        store.mark(shape, key, number)
        if frame.effects and settings.rebind is not None:
            settings.rebind(result, shape, list(frame.effects))
        _events.append(Event(name, "built", built_s, key, number, str(brep)))
        log.info("built %s (%.2fs)", name, built_s)
        return shape

    def __repr__(self):
        return "<cadcache part %s>" % _name(self.fn)


def part(fn=None, *, version=None, part_class=None):
    """Cache what fn returns. Use as @part or @part(version=2); bumping
    `version` forces a rebuild without changing the code. part_class is
    the class its part numbers are hashed under (default: the function's
    name, with hyphens)."""
    if fn is None:
        return lambda f: Part(f, version, part_class)
    return Part(fn, version, part_class)


_RUNTIME = None


def _runtime():
    """Versions that change what a build or a stored BREP holds."""
    global _RUNTIME
    if _RUNTIME is None:
        import build123d
        try:
            from importlib.metadata import version
            ocp = version("cadquery-ocp")
        except Exception:
            ocp = ""
        _RUNTIME = {"build123d": build123d.__version__, "ocp": ocp,
                    "python": "%d.%d" % sys.version_info[:2]}
    return _RUNTIME


def assembly(fn):
    """Mark a function that only places parts. It is never cached - the
    parts it calls are - and shows in events() as an assembly."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            _events.append(Event(_name(fn), "assembly",
                                 time.perf_counter() - t0))
    wrapper.uncached = fn
    wrapper.__cadcache__ = "assembly"
    return wrapper
