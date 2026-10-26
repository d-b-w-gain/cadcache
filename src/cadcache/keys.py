"""
Cache keys.

A part's key is the SHA-256 of canonical JSON holding:

    the call's arguments, bound to the signature with defaults applied;
    the source of the part function and of every function or class it
    reaches in the project (helpers, other parts), as an AST dump, so
    comments, blank lines and moved code don't count;
    the values of plain-data globals those functions read;
    the build123d version and the cadcache key format.

Arguments must be values cadcache knows how to key: numbers, text, lists,
dicts, dataclasses, enums, pathlib.Path (keyed on file content), build123d
vectors, locations, axes, planes, colours and shapes, functions (keyed on
their code), partnum requirements, or anything with __cadcache_key__().
Other types raise TypeError; register_key() adds one.
"""
import ast
import dataclasses
import dis
import enum
import hashlib
import inspect
import json
import logging
import math
import os
import site
import sys
import sysconfig
import textwrap
import types
import warnings
from decimal import Decimal
from fractions import Fraction
from pathlib import Path, PurePath

FORMAT = 1


class CacheWarning(UserWarning):
    """Something a part reads isn't one of its arguments."""


class GlobalReadWarning(CacheWarning):
    """A part (or a helper it calls) reads a module-level value."""


class FileReadWarning(CacheWarning):
    """A part reads a file that isn't one of its arguments."""


# ------------------------------------------------------------- arguments
_KEYERS = {}


def register_key(cls, fn):
    """Key arguments of type `cls` (and subclasses) on fn(value), which
    must return something keyable: numbers, text, lists, dicts..."""
    _KEYERS[cls] = fn


def _num(x):
    if isinstance(x, float):
        if math.isnan(x) or math.isinf(x):
            return {"$float": repr(x)}
        if x.is_integer() and abs(x) < 2 ** 53:
            return int(x)
        return x
    return x


def _f(x):
    """Floats from a transform: 12 significant digits, no -0."""
    x = float("%.12g" % x)
    return 0.0 if x == 0 else x


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _b3d(v):
    """Key build123d values, or None if v isn't one."""
    mod = type(v).__module__
    if not mod.startswith("build123d"):
        return None
    import build123d as b
    if isinstance(v, b.Vector):
        return {"$vec": [_f(c) for c in v]}
    if isinstance(v, b.Location):
        return {"$loc": location_values(v)}
    if isinstance(v, b.Axis):
        return {"$axis": [[_f(c) for c in v.position],
                          [_f(c) for c in v.direction]]}
    if isinstance(v, b.Plane):
        return {"$plane": [[_f(c) for c in v.origin],
                           [_f(c) for c in v.x_dir],
                           [_f(c) for c in v.z_dir]]}
    if isinstance(v, b.Color):
        return {"$color": [_f(c) for c in tuple(v)]}
    if isinstance(v, b.Shape):
        return shape_key(v)
    return None


def location_values(loc):
    t = loc.wrapped.Transformation()
    return [_f(t.Value(i, j)) for i in range(1, 4) for j in range(1, 5)]


def shape_key(shape):
    """A shape returned by a part is keyed on that part's key and where it
    has been moved to since; any other shape on its BREP text."""
    k = getattr(shape, "_cadcache_key", None)
    if k is not None and getattr(shape, "_cadcache_loc", None) == \
            location_values(shape.location):
        return {"$part": k}
    import io
    from build123d import export_brep
    buf = io.BytesIO()
    export_brep(shape, buf)
    return {"$brep": _sha(buf.getvalue())}


def _partnum(v):
    """partnum Requirements: the part number, and a hash of everything in
    the file, so a colour or name change rebuilds too."""
    data = json.dumps(v.data, sort_keys=True, default=str,
                      ensure_ascii=False)
    return {"$partnum": v.id, "data": _sha(data.encode("utf-8"))}


def requirements_key(req):
    """What a partnum requirements argument (or a path to its file)
    contributes to a key."""
    if isinstance(req, (str, PurePath)):
        from partnum import load_requirements
        req = load_requirements(req)
    return _partnum(req)


def canon(v, where="argument"):
    """Plain JSON data standing for v. TypeError if v can't be keyed."""
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, (int, float)):
        return _num(v)
    if isinstance(v, (Decimal, Fraction)):
        return _num(float(v))
    if isinstance(v, enum.Enum):
        return {"$enum": "%s.%s" % (type(v).__qualname__, v.name)}
    for cls, fn in _KEYERS.items():
        if isinstance(v, cls):
            return {"$custom": type(v).__qualname__,
                    "key": canon(fn(v), where)}
    if hasattr(v, "__cadcache_key__"):
        return {"$obj": type(v).__qualname__,
                "key": canon(v.__cadcache_key__(), where)}
    b = _b3d(v)
    if b is not None:
        return b
    if isinstance(v, PurePath):
        p = Path(v)
        if p.is_file():
            return {"$file": p.name, "sha256": file_sha(p)}
        return {"$path": p.as_posix()}
    if isinstance(v, (bytes, bytearray)):
        return {"$bytes": _sha(bytes(v))}
    if isinstance(v, (list, tuple)):
        return [canon(x, where) for x in v]
    if isinstance(v, (set, frozenset)):
        return {"$set": sorted((canon(x, where) for x in v), key=_dumps)}
    if isinstance(v, dict):
        items = [[canon(k, where), canon(x, where)] for k, x in v.items()]
        return {"$dict": sorted(items, key=lambda kv: _dumps(kv[0]))}
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {"$dc": type(v).__qualname__,
                "fields": {f.name: canon(getattr(v, f.name), where)
                           for f in dataclasses.fields(v)}}
    if type(v).__module__.startswith("partnum") and hasattr(v, "full_hash"):
        return _partnum(v)
    if isinstance(v, (types.FunctionType, type)):
        return {"$code": code_hash(v)[0]}
    raise TypeError(
        "cadcache can't key %s of type %s. Pass plain values, give the "
        "type a __cadcache_key__() method or register_key() it, or call "
        "the part's .uncached(...)" % (where, type(v).__qualname__))


def _dumps(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def digest(data):
    return _sha(_dumps(data).encode("utf-8"))


# ------------------------------------------------------------------ code
_LIB_DIRS = None


def _lib_dirs():
    global _LIB_DIRS
    if _LIB_DIRS is None:
        dirs = set()
        for key in ("stdlib", "platstdlib", "purelib", "platlib"):
            try:
                dirs.add(sysconfig.get_paths()[key])
            except KeyError:
                pass
        try:
            dirs.update(site.getsitepackages())
            dirs.add(site.getusersitepackages())
        except AttributeError:
            pass
        dirs.update({sys.prefix, sys.base_prefix, sys.exec_prefix})
        _LIB_DIRS = tuple(os.path.normcase(os.path.abspath(d)) + os.sep
                          for d in dirs if d)
    return _LIB_DIRS


def is_library_path(path):
    p = os.path.normcase(os.path.abspath(path))
    return p.startswith(_lib_dirs()) or "site-packages" in p


def module_root(module):
    """The folder a module's project lives in: the folder holding its top
    package, or the folder of a plain module or script."""
    top = sys.modules.get(module.__name__.split(".")[0], module)
    f = getattr(top, "__file__", None) or getattr(module, "__file__", None)
    if not f:
        return None
    f = Path(f).resolve()
    if f.name == "__init__.py":
        return f.parent.parent
    return f.parent


def _is_local(obj, root):
    if isinstance(obj, types.ModuleType):
        mod = obj
    else:
        mod = sys.modules.get(getattr(obj, "__module__", None) or "")
    f = getattr(mod, "__file__", None)
    if not f or root is None or is_library_path(f):
        return False
    try:
        Path(f).resolve().relative_to(root)
    except ValueError:
        return False
    return True


_OWN = {}        # id(code or class) -> (object, hash)
_REFS = {}       # id(code) -> (code, [(name, attrs)])


def _source_text(obj):
    try:
        src = textwrap.dedent(inspect.getsource(obj))
        return ast.dump(ast.parse(src))
    except (OSError, TypeError, SyntaxError, IndentationError):
        code = getattr(obj, "__code__", None)
        if code is None:
            raise TypeError("cadcache can't read the source of %r" % (obj,))
        return _code_text(code)


def _code_text(code):
    """Source fallback (REPL, exec): bytecode and constants, no line
    numbers or file names."""
    consts = [_code_text(c) if isinstance(c, types.CodeType) else repr(c)
              for c in code.co_consts]
    return repr((code.co_code, consts, code.co_names, code.co_varnames))


def _own_hash(obj):
    k = getattr(obj, "__code__", obj)
    hit = _OWN.get(id(k))
    if hit is not None and hit[0] is k:
        return hit[1]
    h = _sha(_source_text(obj).encode("utf-8"))
    _OWN[id(k)] = (k, h)
    return h


_LOADS = {"LOAD_GLOBAL", "LOAD_NAME"}
_ATTRS = {"LOAD_ATTR", "LOAD_METHOD"}


def _refs(code):
    """(global name, attribute read on it or None) for code and every
    function, lambda and comprehension nested in it."""
    hit = _REFS.get(id(code))
    if hit is not None and hit[0] is code:
        return hit[1]
    out = []
    ins = list(dis.get_instructions(code))
    for i, x in enumerate(ins):
        if x.opname in _LOADS:
            nxt = ins[i + 1] if i + 1 < len(ins) else None
            attr = nxt.argval if nxt is not None and nxt.opname in _ATTRS \
                else None
            out.append((x.argval, attr))
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            out.extend(_refs(c))
    _REFS[id(code)] = (code, out)
    return out


def _codes(obj):
    """(code, globals, closure values) for a function, or each method of
    a class."""
    if isinstance(obj, type):
        for v in vars(obj).values():
            if isinstance(v, (staticmethod, classmethod)):
                v = v.__func__
            elif isinstance(v, property):
                for f in (v.fget, v.fset, v.fdel):
                    if f is not None:
                        yield from _codes(f)
                continue
            if isinstance(v, types.FunctionType):
                yield from _codes(v)
        return
    fn = inspect.unwrap(obj)
    code = getattr(fn, "__code__", None)
    if code is None:
        return
    cells = {}
    for name, cell in zip(code.co_freevars, fn.__closure__ or ()):
        try:
            cells[name] = cell.cell_contents
        except ValueError:
            pass
    yield code, fn.__globals__, cells


def _library_value(name, value, globs):
    """True if `value` is the same object as `name` in a library module
    the code's module imported from (build123d's MM, math's pi...)."""
    seen = set()
    for v in list(globs.values()):
        if isinstance(v, types.ModuleType):
            mod = v
        else:
            mod = sys.modules.get(getattr(v, "__module__", None) or "")
        if mod is None:
            continue
        top = sys.modules.get(mod.__name__.split(".")[0])
        for m in (mod, top):
            if m is None or id(m) in seen:
                continue
            seen.add(id(m))
            if m is not sys.modules.get(globs.get("__name__")) and \
                    getattr(m, name, _MISSING) is value:
                return True
    return False


_MISSING = object()
OCCT_READERS = {"import_step", "import_brep", "import_stl", "import_svg",
                "import_three_mf", "import_dxf", "Mesher"}


def _ignorable(value):
    return isinstance(value, (types.ModuleType, logging.Logger,
                              types.BuiltinFunctionType))


def code_hash(fn, root=None, report=None):
    """Hash of fn's code and everything it reaches in the project.

    report(kind, name, where), if given, is told about globals that hold
    data ("global") and calls to OCCT file readers ("occt")."""
    if root is None:
        mod = sys.modules.get(fn.__module__)
        root = module_root(mod) if mod else None
    hashes, data = {}, {}
    stack = [inspect.unwrap(fn)]
    while stack:
        obj = stack.pop()
        name = "%s.%s" % (obj.__module__, obj.__qualname__)
        if name in hashes:
            continue
        hashes[name] = _own_hash(obj)
        for code, globs, cells in _codes(obj):
            values = []
            for gname, attr in _refs(code):
                if gname not in globs:
                    continue                    # builtin
                v = globs[gname]
                if isinstance(v, types.ModuleType) and attr:
                    if not _is_local(v, root) or \
                            not hasattr(v, "__file__"):
                        if attr in OCCT_READERS and report:
                            report("occt", attr, name)
                        continue
                    sub = getattr(v, attr, _MISSING)
                    if sub is _MISSING:
                        continue
                    values.append(("%s.%s" % (gname, attr), sub,
                                   v.__dict__))
                else:
                    values.append((gname, v, globs))
            values.extend((c, v, None) for c, v in cells.items())
            for vname, v, ns in values:
                if vname.split(".")[-1] in OCCT_READERS and report:
                    report("occt", vname, name)
                target = inspect.unwrap(v) if callable(v) else v
                if isinstance(target, (types.FunctionType, type)):
                    if _is_local(target, root):
                        stack.append(target)
                    continue
                if _ignorable(v) or callable(v):
                    continue
                if ns is not None and _library_value(
                        vname.split(".")[-1], v, ns):
                    continue
                key = "%s:%s" % (name, vname)
                try:
                    data[key] = canon(v, "global %s" % vname)
                except TypeError:
                    data[key] = {"$untracked": type(v).__qualname__}
                    if report:
                        report("untracked", vname, name)
                    continue
                if report:
                    report("global", vname, name)
    return digest({"code": hashes, "globals": data}), sorted(hashes)
