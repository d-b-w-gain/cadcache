# cadcache

Per-part caching for build123d, keyed on input hashes, like make or Bazel.

Mark a function that builds a part with `@part`. The first call with given
inputs builds the part and stores it. Later calls with the same inputs load
it from the cache. If one part in a twenty-part model changes, a rerun
rebuilds that one part and loads the rest.

Nothing about build123d changes. A part function is plain build123d code,
and every call returns a real build123d `Part`/`Shape`, built or loaded.
Booleans, selectors, joints, exporters and ocp_vscode all work on it as
usual.

## Example: two parts and an assembly

The dincase slide roller and its sleeve ([examples/dincase_roller.py](
examples/dincase_roller.py), specs copied from RolaCase):

```python
from pathlib import Path
from build123d import Align, Color, Compound, Cylinder, Location, Part, RigidJoint
from partnum import load_requirements
from cadcache import assembly, part

SPECS = Path(__file__).parent / "specs"


def tube(od, bore, length, z=0):
    up = (Align.CENTER, Align.CENTER, Align.MIN)
    return Location((0, 0, z)) * (Cylinder(od / 2, length, align=up) -
                                  Cylinder(bore / 2, length, align=up))


def colour(req):
    return Color(*req.get("appearance", {}).get("colour", (0.7, 0.7, 0.7)))


@part
def slide_roller(req):                  # 30 dia, 4 rim, 0.5 hub of 12, 8 bore
    hub_h, rim_h = req["hub_h_mm"], req["roller_h_mm"]
    roller = Part() + [tube(req["hub_d_mm"], req["bore_d_mm"], hub_h),
                       tube(req["roller_d_mm"], req["bore_d_mm"], rim_h, z=hub_h)]
    roller.label, roller.color = "slide roller " + req.id, colour(req)
    RigidJoint("axle", roller, Location())
    return roller


@part
def roller_sleeve(req, roller_req):
    od = roller_req["bore_d_mm"] - req["running_fit_mm"]
    length = roller_req["hub_h_mm"] + roller_req["roller_h_mm"]
    sleeve = Part() + tube(od, req["bore_d_mm"], length)
    sleeve.label, sleeve.color = "roller sleeve " + req.id, colour(req)
    RigidJoint("axle", sleeve, Location())
    return sleeve


@assembly
def roller_on_sleeve(roller, sleeve):
    sleeve.joints["axle"].connect_to(roller.joints["axle"])
    return Compound(children=[sleeve, roller], label="slide roller")


roller_req = load_requirements(SPECS / "slide-roller.yaml")
sleeve_req = load_requirements(SPECS / "slide-sleeve.yaml")
asm = roller_on_sleeve(slide_roller(roller_req),
                       roller_sleeve(sleeve_req, roller_req))
```

```
> cadcache watch examples/dincase_roller.py
cadcache: built slide_roller (0.02s)
cadcache: built roller_sleeve (0.01s)
cadcache: ran in 4.30s - 2 parts: 0 cached, 2 built
cadcache: changed dincase_roller.py                 (a comment)
cadcache: ran in 0.07s - 2 parts: 2 cached, 0 built
cadcache: changed dincase_roller.py                 (the sleeve's length)
cadcache: built roller_sleeve (0.01s)
cadcache: ran in 0.09s - 2 parts: 1 cached, 1 built
```

The joints on the loaded parts still connect. The assembly runs every time,
because placing parts is cheap.

## What a part's key covers

A part rebuilds when any of these change:

- **its arguments**, bound to the signature with defaults filled in, so
  `f(10)`, `f(w=10)` and `f(10.0)` share an entry;
- **its code, and the code it reaches in your project**: helper
  functions and classes, other parts, and functions in other modules of
  the project. Code is hashed as a syntax tree, so comments, blank lines
  and code moving within the file don't trigger a rebuild. Installed
  libraries aren't hashed;
- **plain-data globals** those functions read (with a warning, below);
- **files the part read** while building, checked by content on every
  hit. A file a nested part read counts for the outer part too;
- the **build123d version**, and `@part(version=N)` to force a rebuild by
  hand.

Arguments must be values cadcache can key: numbers, text, lists, tuples,
dicts, sets, enums, dataclasses, `pathlib.Path` (keyed on the file's
content), build123d `Vector`, `Location`, `Axis`, `Plane`, `Color` and
shapes, functions (keyed on their code), partnum requirements, or any
object with a `__cadcache_key__()` method. `register_key(cls, fn)` adds a
type. Any other argument raises `TypeError` instead of being guessed at. A
shape that came out of another part keys on that part's key, unless it has
been moved since. Any other shape keys on its BREP.

## Keyed on a partnum requirements file

Pass the loaded requirements in as an argument, as above. The key is the
part number plus a hash of everything in the file, so an edit to any field
rebuilds that part. This includes `appearance`, which the part number
leaves out. `cadcache.requirements_key(path_or_req)` returns that key
piece on its own.

The older style still works: a module-level `ROLLER = load_requirements(...)`
read inside the part. The requirements are keyed the same way, with a
warning.

## Warnings

cadcache warns, once per part per run, when a part reads something that
isn't one of its arguments:

- `GlobalReadWarning`: a part or a helper reads a module-level value. If
  the value can be keyed, it goes into the key. If it can't, the warning
  says changes to it won't rebuild the part. Library constants such as
  build123d's `MM` or `math.pi` are skipped.
- `FileReadWarning`: a part opened a file that isn't an argument. The file
  is tracked by content either way. The other case is calls to
  `import_step`, `import_stl` and similar: OCCT reads those files outside
  Python, where cadcache can't see them, so pass the path as a
  `pathlib.Path` argument.

Both are `CacheWarning` subclasses, so the standard `warnings` filters
silence them.

## Switches

```python
slide_roller.uncached(req)       # build this one call without the cache
with cadcache.disabled(): ...    # build everything in the block
CADCACHE=off python model.py     # build everything in a run
cadcache.configure(cache_dir=..., root=..., enabled=...)
```

`root` is the project folder. Code under it is hashed into keys; code
outside it is treated as a library. By default it is the folder holding
the part's top-level package, or the script's folder.

`@assembly` marks a function that only places parts. It is never cached,
and it shows up in `events()`.

## On disk

`.cadcache/` in the project folder (or `CADCACHE_DIR`) holds two files per
part:

```
.cadcache/slide_roller/a2bd2d7a0720a19086ed13fe.brep   the shape, plain OCCT BREP
.cadcache/slide_roller/a2bd2d7a0720a19086ed13fe.json   label, colour, material, joints,
                                                       children; key, arguments, files
                                                       read, build time
```

BREP doesn't keep labels, colours or joints, so the JSON sidecar does.
RigidJoint, RevoluteJoint, LinearJoint, CylindricalJoint and BallJoint
come back with their positions and ranges, and compound children come
back with their labels, colours and locations. Each call gets a fresh
object loaded from the BREP. That happens on a build too, so a part
behaves the same whether it was built or loaded. Anything else set on a
shape as a Python attribute doesn't survive. A `Box` subclass comes back
as a `Part`, and a `Solid` as a `Solid`.

The cache grows: a changed part writes a new entry, and old entries stay.
Delete the folder whenever you like.

```
cadcache ls examples/.cadcache
cadcache clear examples/.cadcache
```

## Rerunning on change

```
cadcache watch model.py [script args]    # rerun on every save
cadcache run model.py                    # once, with a summary of the parts
```

The script reruns inside the watching process, so build123d stays
imported. The import alone takes a few seconds, and it's the slow part of
a fresh run. Before each run, the project's own modules are dropped so
edits are picked up. The runner watches the project's `.py`, `.yaml`,
`.json`, `.step` and similar files, plus any files the parts read.
`--fresh` uses a new Python process for every run instead.

## Using it from a GUI

`cadcache.events()` lists each part as built, cached, uncached or an
assembly, with timings. `cadcache.reset()` starts a new run.
`cadcache.summary()` gives a one-line report. `part.key(...)` and
`part.paths(...)` return the key and files for a call without building
anything. `cadcache.watch.run_once(script)` performs one in-process rerun.

## Install

```
pip install -e path/to/cadcache           # while developing
```

partnum is optional. It's only needed for the example and for
requirements arguments, and it installs separately.

Tests: `python -m unittest discover -s tests` (pytest works too).
