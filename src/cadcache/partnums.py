"""
Part numbers for parts, hashed from their inputs.

Every call of a part gets a partnum-style number, AAA-XXXX-XXXXX, made the
same way partnum makes one from a requirements file (scheme pn/1): the
SHA-256 of canonical JSON holding the part's class, its parameters and the
parts it uses. Here the parameters are the call's arguments plus the
plain-data globals the part reads, and `uses` names the parts passed in.

Code is left out on purpose. The cache key changes when the code does, so
a refactor rebuilds the part, but its part number stays the same. Same
inputs, same number, on any computer.

pn/1 is frozen, so its rules are copied here rather than importing partnum
(which stays optional); a test checks the two agree.
"""
import dataclasses
import enum
import hashlib
import json
import math
import unicodedata
from decimal import Decimal
from fractions import Fraction
from pathlib import Path, PurePath

from . import keys

SCHEME = "pn/1"
LETTERS = "ABCDEFGHJKMNPQRSTVWXYZ"            # 22: no I L O U
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # 32: no I L O U


# ------------------------------------------------------------- pn/1 rules
def _canon(v):
    if isinstance(v, dict):
        return {_canon_key(k): _canon(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float, Decimal)):
        d = Decimal(str(v)).normalize()
        return int(d) if d == d.to_integral_value() else float(d)
    if isinstance(v, str):
        return " ".join(unicodedata.normalize("NFC", v).split())
    raise TypeError("unsupported value: %r" % (v,))


def _canon_key(k):
    return " ".join(unicodedata.normalize("NFC", str(k)).lower().split())


def canonical(data):
    payload = {"scheme": SCHEME, "class": data["class"],
               "parameters": data["parameters"],
               "uses": data.get("uses") or {}}
    return json.dumps(_canon(payload), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)


def number_from_digest(hexdigest):
    n = int(hexdigest, 16)
    letters = ""
    for _ in range(3):
        n, r = divmod(n, len(LETTERS))
        letters += LETTERS[r]
    body = ""
    for _ in range(9):
        n, r = divmod(n, 32)
        body += CROCKFORD[r]
    return "%s-%s-%s" % (letters, body[:4], body[4:])


def number(data):
    """(part number, full sha256 hex) for {class, parameters, uses}."""
    digest = hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()
    return number_from_digest(digest), digest


# ------------------------------------------------------- inputs as data
def part_number_of(shape):
    """The part number of a shape a part returned, or of a moved or copied
    instance of it: placing a part doesn't make it a different part. A
    boolean result is a new shape and has none. None for other shapes."""
    return getattr(shape, "_cadcache_pn", None)


def of_shape(shape):
    """The part number of a shape a part returned, while it hasn't been
    moved; None for any other shape."""
    pn = getattr(shape, "_cadcache_pn", None)
    if pn is not None and getattr(shape, "_cadcache_loc", None) == \
            keys.location_values(shape.location):
        return pn
    return None


def _requirements_id(v):
    if type(v).__module__.startswith("partnum") and hasattr(v, "full_hash"):
        return v.id
    return None


def value(v):
    """v as pn/1 data. Like keys.canon, except that nothing here depends
    on code: a shape from a part stands for its part number, a partnum
    requirements object for its own number, a function for its name."""
    if v is None or isinstance(v, (bool, str, int)):
        return v
    if isinstance(v, (float, Decimal, Fraction)):
        v = float(v)
        return v if math.isfinite(v) else {"$float": repr(v)}
    if isinstance(v, enum.Enum):
        return {"$enum": "%s.%s" % (type(v).__qualname__, v.name)}
    for cls, fn in keys._KEYERS.items():
        if isinstance(v, cls):
            return {"$custom": type(v).__qualname__, "key": value(fn(v))}
    if hasattr(v, "__cadcache_key__"):
        return {"$obj": type(v).__qualname__,
                "key": value(v.__cadcache_key__())}
    req = _requirements_id(v)
    if req is not None:
        return {"$part": req}
    try:
        import build123d as b
    except ImportError:                 # pragma: no cover
        b = None
    if b is not None and isinstance(v, b.Shape):
        pn = getattr(v, "_cadcache_pn", None)
        if pn is None:
            return keys.shape_key(v)
        if of_shape(v) == pn:
            return {"$part": pn}
        return {"$part": pn, "at": keys.location_values(v.location)}
    special = keys._b3d(v)
    if special is not None:
        return special
    if isinstance(v, PurePath):
        p = Path(v)
        if p.is_file():
            return {"$file": keys.file_sha(p)}
        return {"$path": p.as_posix()}
    if isinstance(v, (bytes, bytearray)):
        return {"$bytes": keys._sha(bytes(v))}
    if isinstance(v, (list, tuple)):
        return [value(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return sorted((value(x) for x in v), key=keys._dumps)
    if isinstance(v, dict):
        if all(isinstance(k, str) for k in v):
            return {k: value(x) for k, x in v.items()}
        return sorted(([value(k), value(x)] for k, x in v.items()),
                      key=lambda kv: keys._dumps(kv[0]))
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {"$dc": type(v).__qualname__,
                "fields": {f.name: value(getattr(v, f.name))
                           for f in dataclasses.fields(v)}}
    if callable(v) and hasattr(v, "__qualname__"):
        return {"$code": "%s.%s" % (getattr(v, "__module__", ""),
                                    v.__qualname__)}
    raise TypeError("can't make a part number from %s"
                    % type(v).__qualname__)


def part_number(part_class, arguments, global_values=None):
    """The number for one call: arguments that are parts (or partnum
    requirements) go under `uses`, everything else under `parameters`,
    with the plain-data globals the part reads under `$globals`."""
    params, uses = {}, {}
    for name, v in arguments.items():
        ref = _requirements_id(v)
        if ref is None and getattr(v, "_cadcache_pn", None) is not None:
            ref = of_shape(v)
        if ref is not None:
            uses[name] = ref
        else:
            params[name] = value(v)
    if global_values:
        params["$globals"] = {k: value(v) for k, v in
                              sorted(global_values.items())}
    return number({"class": part_class, "parameters": params,
                   "uses": uses})[0]
