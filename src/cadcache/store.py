"""
Cache entries on disk.

Each entry is two files in <cache dir>/<function name>/:

    <key>.brep   the shape, as OCCT writes it (any BREP reader opens it)
    <key>.json   the sidecar: what BREP drops - label, colour, material,
                 joints, the children of a compound - plus the key, the
                 files the build read, and how long it took

The sidecar is written last, so an entry without one is unfinished and is
ignored. Both are written to a temporary name and renamed into place.
"""
import importlib
import json
import os
import re
import tempfile
from pathlib import Path

from .keys import location_values

SIDECAR_VERSION = 2


def entry_paths(cache_dir, fn_name, key):
    folder = Path(cache_dir) / re.sub(r"[^\w.-]+", "_", fn_name)
    return folder / (key[:24] + ".brep"), folder / (key[:24] + ".json")


def read_sidecar(json_path):
    try:
        return json.loads(Path(json_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _atomic(path, write):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.stem,
                               suffix=".tmp")
    os.close(fd)
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def snapshot(shape, meta, attributes=()):
    """Everything an entry stores about a shape, taken now: its OCCT shape
    as the build123d tree stands (OCCT shapes don't change once made) and
    its sidecar. Cheap next to writing; later changes to the shape don't
    reach it."""
    return (_tree_topods(shape),
            dict(meta, sidecar=SIDECAR_VERSION,
                 shape=describe(shape, attributes)))


def write(snap, brep_path, json_path):
    """Write a snapshot as an entry: BREP first, sidecar (the completion
    marker) last."""
    from OCP.BRepTools import BRepTools
    topods, meta = snap

    def brep(tmp):
        # What build123d's export_brep does, for the snapshot's shape.
        if BRepTools.Write_s(topods, tmp) is False:
            raise RuntimeError("BREP export failed for %s" % brep_path)

    _atomic(brep_path, brep)
    text = json.dumps(meta, indent=1) + "\n"
    _atomic(json_path,
            lambda tmp: Path(tmp).write_text(text, encoding="utf-8"))


def save(shape, meta, brep_path, json_path, attributes=()):
    write(snapshot(shape, meta, attributes), brep_path, json_path)


def from_snapshot(snap):
    """A fresh shape rebuilt from a snapshot, as load() rebuilds one from an
    entry on disk."""
    from build123d import Compound
    topods, meta = snap
    return rebuild(Compound.cast(topods), meta["shape"])


def load(brep_path, meta):
    from build123d import import_brep
    raw = import_brep(str(brep_path))
    return rebuild(raw, meta["shape"])


# ------------------------------------------------------- shape metadata
def _class_name(obj):
    return "%s.%s" % (type(obj).__module__, type(obj).__qualname__)


def describe(shape, attributes=()):
    """The parts of a shape BREP doesn't keep, as JSON. attributes: names
    of plain-data Python attributes to keep as well, where they are set
    and JSON can hold them."""
    color = getattr(shape, "color", None)
    node = {
        "type": _class_name(shape),
        "label": getattr(shape, "label", "") or "",
        "color": list(tuple(color)) if color is not None else None,
        "material": getattr(shape, "material", "") or "",
        "joints": [_joint(j) for j in
                   (getattr(shape, "joints", None) or {}).values()],
    }
    attrs = {}
    # A part built inside another keeps its marks, so a hit on the outer
    # part gives back nested parts that still know their key and part
    # number, as if each had been called.
    for name in (*_PART_MARKS, *attributes):
        v = getattr(shape, name, None)
        if v is None:
            continue
        try:
            attrs[name] = json.loads(json.dumps(v, allow_nan=False))
        except (TypeError, ValueError):
            continue                    # not plain data; not kept
    if attrs:
        node["attrs"] = attrs
    kids = _stored_children(shape)
    if kids:
        node["children"] = [describe(k, attributes) for k in kids]
    return node


_PART_MARKS = ("_cadcache_key", "_cadcache_pn", "_cadcache_name", "_cadcache_loc")


def _stored_children(shape):
    return [k for k in (getattr(shape, "children", None) or ())
            if k.wrapped is not None]


def _tree_topods(shape):
    """The shape's OCCT shape as its build123d tree stands now: a compound
    of its children's, recursively, at its own location. A parent's own
    `wrapped` is a snapshot from when it was built, and lags the tree once
    a child has been re-parented elsewhere, so it can't be stored as is:
    the sidecar describes the tree, and the two must match on load."""
    kids = _stored_children(shape)
    if not kids:
        return shape.wrapped
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound
    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    for kid in kids:
        builder.Add(compound, _tree_topods(kid))
    if shape.wrapped is not None:
        compound = compound.Located(shape.wrapped.Location())
    return compound


def _joint(j):
    attrs = {}
    for name, v in vars(j).items():
        if name in ("label", "parent", "connected_to"):
            continue
        try:
            attrs[name] = _enc(v)
        except TypeError:
            continue                    # not restorable; left unset
    return {"type": _class_name(j), "label": j.label, "attrs": attrs}


def _enc(v):
    import build123d as b
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, tuple):
        return {"$tuple": [_enc(x) for x in v]}
    if isinstance(v, list):
        return [_enc(x) for x in v]
    if isinstance(v, b.Location):
        return {"$loc": _exact(v)}
    if isinstance(v, b.Axis):
        return {"$axis": [list(v.position), list(v.direction)]}
    if isinstance(v, b.Plane):
        return {"$plane": [list(v.origin), list(v.x_dir), list(v.z_dir)]}
    if isinstance(v, b.Vector):
        return {"$vec": list(v)}
    raise TypeError(type(v))


def _exact(loc):
    t = loc.wrapped.Transformation()
    return [t.Value(i, j) for i in range(1, 4) for j in range(1, 5)]


def _dec(v):
    import build123d as b
    if isinstance(v, list):
        return [_dec(x) for x in v]
    if not isinstance(v, dict):
        return v
    if "$tuple" in v:
        return tuple(_dec(x) for x in v["$tuple"])
    if "$loc" in v:
        from OCP.gp import gp_Trsf
        t = gp_Trsf()
        t.SetValues(*v["$loc"])
        return b.Location(t)
    if "$axis" in v:
        return b.Axis(*v["$axis"])
    if "$plane" in v:
        o, x, z = v["$plane"]
        return b.Plane(origin=o, x_dir=x, z_dir=z)
    if "$vec" in v:
        return b.Vector(*v["$vec"])
    return v


def _import(dotted):
    mod, _, name = dotted.rpartition(".")
    obj = importlib.import_module(mod)
    for part in name.split("."):
        obj = getattr(obj, part)
    return obj


def _b3d_class(dotted, fallback):
    """The class to rebuild as: the stored one if it is build123d's own
    topology class, else the nearest one it derives from (a Box comes back
    as a Part; Box() wants dimensions, not a shape)."""
    try:
        cls = _import(dotted)
    except (ImportError, AttributeError):
        return fallback
    for c in cls.__mro__:
        if c.__module__.startswith("build123d.topology"):
            return c
    return fallback


def _subshapes(raw):
    """Direct sub-shapes, each keeping its own location (not combined
    with the compound's)."""
    from OCP.TopoDS import TopoDS_Iterator
    from build123d import Compound
    it = TopoDS_Iterator(raw.wrapped, True, False)
    while it.More():
        yield Compound.cast(it.Value())
        it.Next()


def rebuild(raw, node):
    from build123d import Color, Compound, Location
    cls = _b3d_class(node["type"], type(raw))
    kids = node.get("children")
    if kids:
        subs = list(_subshapes(raw))
        if len(subs) != len(kids):
            raise ValueError("sidecar has %d children, BREP has %d"
                             % (len(kids), len(subs)))
        children = [rebuild(s, n) for s, n in zip(subs, kids)]
        if not issubclass(cls, Compound):
            cls = Compound
        shape = cls(children=children)
        shape.location = Location(raw.wrapped.Location())
    else:
        try:
            shape = cls(raw.wrapped)
        except TypeError:
            shape = raw
    shape.label = node.get("label", "")
    if node.get("color") is not None:
        shape.color = Color(*node["color"])
    if node.get("material") and hasattr(shape, "material"):
        shape.material = node["material"]
    for j in node.get("joints", ()):
        _restore_joint(shape, j)
    for name, v in (node.get("attrs") or {}).items():
        setattr(shape, name, v)
    return shape


def _restore_joint(shape, data):
    if not hasattr(shape, "joints"):
        return
    try:
        cls = _import(data["type"])
    except (ImportError, AttributeError):
        return
    j = cls.__new__(cls)
    for name, v in data["attrs"].items():
        setattr(j, name, _dec(v))
    j.label = data["label"]
    j.parent = shape
    j.connected_to = None
    shape.joints[j.label] = j


def mark(shape, key, part_number=None, name=None):
    """Tag a shape a part returned, so passing it to another part keys on
    `key` instead of re-hashing its BREP, and its part number counts as an
    input of that part (until it is moved)."""
    shape._cadcache_key = key
    shape._cadcache_pn = part_number
    shape._cadcache_name = name
    shape._cadcache_loc = location_values(shape.location)
