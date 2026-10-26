"""Run with: python -m unittest discover -s tests"""
import importlib
import math
import shutil
import sys
import tempfile
import textwrap
import unittest
import warnings
from pathlib import Path

from build123d import Box, Color, Compound, Location, Part, RigidJoint, \
    RevoluteJoint, Axis, Cylinder

import cadcache
from cadcache import keys, watch

HERE = Path(__file__).resolve().parent
# Modules here are rewritten within a second at the same size, which a
# .pyc can't tell apart (it records whole-second mtimes).
sys.dont_write_bytecode = True
EXAMPLES = HERE.parent / "examples"

PARTS = '''
from build123d import Box, Cylinder, Compound, Location
from cadcache import part, assembly


def fillet_size(x):
    return x / 10


@part
def block(w):
    b = Box(w, w, w)
    b.label = "block"
    return b


@part
def peg(d):
    # a peg
    return Cylinder(d / 2, 10)


@part
def rounded(w):
    from build123d import fillet
    b = Box(w, w, w)
    return fillet(b.edges(), fillet_size(w))


@assembly
def both(w, d):
    return Compound(children=[block(w), peg(d).moved(Location((50, 0, 0)))])
'''


class Project(unittest.TestCase):
    """A throwaway project folder with a module of parts in it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        cadcache.configure(cache_dir=self.root / ".cadcache", root=self.root)
        sys.path.insert(0, str(self.root))
        self.n = 0
        cadcache.reset()
        warnings.simplefilter("ignore", cadcache.CacheWarning)

    def tearDown(self):
        sys.path.remove(str(self.root))
        for name in [m for m in sys.modules if m.startswith("proj_")]:
            del sys.modules[name]
        cadcache.core.settings.root = None
        cadcache.core.settings.cache_dir = None
        cadcache.core.settings.salt = None
        cadcache.core.settings.before_save = None
        cadcache.core.settings.attributes = ()
        warnings.resetwarnings()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def module(self, source, name="proj_parts"):
        """Write (or rewrite) a module and import it afresh, as the watch
        runner does after an edit."""
        (self.root / (name + ".py")).write_text(textwrap.dedent(source),
                                                encoding="utf-8")
        sys.modules.pop(name, None)
        importlib.invalidate_caches()
        return importlib.import_module(name)

    def kinds(self):
        """{part name: kind} for this run, then start a new run."""
        out = {e.name: e.kind for e in cadcache.events()}
        cadcache.reset()
        return out


class Caching(Project):
    def test_second_call_is_a_hit_and_a_real_part(self):
        m = self.module(PARTS)
        a = m.block(10)
        b = m.block(10)
        self.assertEqual([e.kind for e in cadcache.events()],
                         ["built", "cached"])
        self.assertIsInstance(b, Part)
        self.assertAlmostEqual(b.volume, 1000, places=6)
        self.assertEqual(b.label, "block")
        # Booleans, selectors and exporters work on a cached part.
        cut = b - Cylinder(2, 20)
        self.assertLess(cut.volume, 1000)
        self.assertEqual(len(b.faces()), 6)
        self.assertEqual(len(b.edges().filter_by(Axis.Z)), 4)
        # Each call gets its own object.
        a.label = "changed"
        b.move(Location((5, 0, 0)))
        self.assertEqual(m.block(10).label, "block")
        self.assertEqual(m.block(10).location.position.X, 0)

    def test_changed_argument_rebuilds_only_that_part(self):
        m = self.module(PARTS)
        m.both(10, 4)
        self.assertEqual(self.kinds(), {"block": "built", "peg": "built",
                                        "both": "assembly"})
        m.both(10, 4)
        self.assertEqual(self.kinds(), {"block": "cached", "peg": "cached",
                                        "both": "assembly"})
        m.both(10, 5)
        self.assertEqual(self.kinds(), {"block": "cached", "peg": "built",
                                        "both": "assembly"})
        m.both(10.0, 5.0)          # 10 == 10.0
        self.assertEqual(self.kinds(), {"block": "cached", "peg": "cached",
                                        "both": "assembly"})

    def test_keyword_and_default_arguments_key_the_same(self):
        m = self.module(PARTS)
        self.assertEqual(m.block.key(10), m.block.key(w=10))

    def test_changed_source_rebuilds_only_that_part(self):
        m = self.module(PARTS)
        m.both(10, 4)
        self.kinds()
        m = self.module(PARTS.replace("Cylinder(d / 2, 10)",
                                      "Cylinder(d / 2, 12)"))
        m.both(10, 4)
        self.assertEqual(self.kinds(), {"block": "cached", "peg": "built",
                                        "both": "assembly"})

    def test_comments_and_moved_code_dont_rebuild(self):
        m = self.module(PARTS)
        m.both(10, 4)
        self.kinds()
        edited = "\n\n# a new comment\nX_UNUSED = 1\n" + PARTS.replace(
            "# a peg", "# a round peg")
        m = self.module(edited)
        m.both(10, 4)
        self.assertEqual(self.kinds(), {"block": "cached", "peg": "cached",
                                        "both": "assembly"})

    def test_changed_helper_rebuilds_the_parts_using_it(self):
        m = self.module(PARTS)
        m.rounded(10), m.block(10)
        self.kinds()
        m = self.module(PARTS.replace("return x / 10", "return x / 8"))
        m.rounded(10), m.block(10)
        self.assertEqual(self.kinds(), {"rounded": "built",
                                        "block": "cached"})

    def test_helper_in_another_module(self):
        self.module("def size(x):\n    return x * 2\n", "proj_helpers")
        src = ("from build123d import Box\nfrom cadcache import part\n"
               "import proj_helpers\n\n@part\ndef box(w):\n"
               "    return Box(proj_helpers.size(w), w, w)\n")
        m = self.module(src)
        m.box(3)
        self.kinds()
        self.module("def size(x):\n    return x * 3\n", "proj_helpers")
        m = self.module(src)
        b = m.box(3)
        self.assertEqual(self.kinds(), {"box": "built"})
        self.assertAlmostEqual(b.volume, 81, places=6)

    def test_part_calling_a_part(self):
        src = PARTS + textwrap.dedent('''
            @part
            def holed(w, d):
                return block(w) - peg(d)
            ''')
        m = self.module(src)
        m.holed(10, 4)
        self.assertEqual(self.kinds(), {"holed": "built", "block": "built",
                                        "peg": "built"})
        m.holed(10, 4)
        self.assertEqual(self.kinds(), {"holed": "cached"})
        # The inner part's code is part of the outer part's key.
        m = self.module(src.replace("Cylinder(d / 2, 10)",
                                    "Cylinder(d / 2, 12)"))
        m.holed(10, 4)
        self.assertEqual(self.kinds(), {"holed": "built", "block": "cached",
                                        "peg": "built"})

    def test_version_forces_a_rebuild(self):
        m = self.module(PARTS)
        m.block(10)
        self.kinds()
        m = self.module(PARTS.replace("@part\ndef block",
                                      "@part(version=2)\ndef block"))
        m.block(10)
        self.assertEqual(self.kinds(), {"block": "built"})

    def test_cache_files(self):
        m = self.module(PARTS)
        brep, side = m.block.paths(10)
        self.assertFalse(brep.exists())
        m.block(10)
        self.assertTrue(brep.exists() and side.exists())
        self.assertEqual(brep.suffix, ".brep")
        # The BREP opens in plain build123d.
        from build123d import import_brep
        self.assertAlmostEqual(import_brep(str(brep)).volume, 1000, places=6)

    def test_damaged_entry_is_rebuilt(self):
        m = self.module(PARTS)
        m.block(10)
        brep, _ = m.block.paths(10)
        brep.write_text("not a brep")
        self.kinds()
        self.assertAlmostEqual(m.block(10).volume, 1000, places=6)
        self.assertEqual(self.kinds(), {"block": "built"})

    def test_must_return_a_shape(self):
        m = self.module("from cadcache import part\n\n@part\n"
                        "def nothing(x):\n    return x\n")
        with self.assertRaises(TypeError):
            m.nothing(1)

    def test_unkeyable_argument(self):
        m = self.module(PARTS)
        with self.assertRaisesRegex(TypeError, "can't key"):
            m.block(object())


class Switches(Project):
    def test_uncached(self):
        m = self.module(PARTS)
        m.block.uncached(10)
        self.assertFalse(m.block.paths(10)[0].exists())
        self.assertEqual(cadcache.events(), [])

    def test_global_off_switch(self):
        m = self.module(PARTS)
        with cadcache.disabled():
            m.block(10), m.block(10)
        self.assertEqual([e.kind for e in cadcache.events()],
                         ["uncached", "uncached"])
        self.assertFalse(m.block.paths(10)[0].exists())
        m.block(10)
        self.assertEqual(cadcache.events()[-1].kind, "built")

    def test_assembly_is_never_cached(self):
        m = self.module(PARTS)
        for _ in range(2):
            asm = m.both(10, 4)
        self.assertEqual([e.kind for e in cadcache.events()
                          if e.name == "both"], ["assembly", "assembly"])
        self.assertFalse((self.root / ".cadcache" / "both").exists())
        self.assertEqual(len(asm.children), 2)


class Metadata(Project):
    def test_label_colour_joints_children(self):
        src = '''
            from build123d import (Axis, Box, Color, Compound, Cylinder,
                                   Location, RevoluteJoint, RigidJoint)
            from cadcache import part

            @part
            def hinge():
                leaf = Box(20, 2, 30)
                leaf.label, leaf.color = "leaf", Color(0.9, 0.5, 0.1)
                RigidJoint("mount", leaf, Location((0, 1, 15), (90, 0, 0)))
                RevoluteJoint("pin", leaf, Axis((10, 0, 0), (0, 0, 1)),
                              angular_range=(0, 180))
                return leaf

            @part
            def pair():
                a = Box(1, 1, 1)
                a.label = "a"
                b = Cylinder(1, 2).moved(Location((5, 0, 0)))
                b.label, b.color = "b", Color("blue")
                c = Compound(children=[a, b], label="pair")
                c.location = Location((0, 0, 10))
                return c
            '''
        m = self.module(src)
        built = m.hinge()
        cached = m.hinge()
        self.assertEqual(self.kinds(), {"hinge": "cached"})
        for leaf in (built, cached):
            self.assertEqual(leaf.label, "leaf")
            self.assertEqual(tuple(leaf.color), (0.9, 0.5, 0.1, 1.0))
            self.assertEqual(set(leaf.joints), {"mount", "pin"})
            mount = leaf.joints["mount"].location
            self.assertAlmostEqual(mount.position.Z, 15)
            self.assertAlmostEqual(mount.orientation.X, 90)
            pin = leaf.joints["pin"]
            self.assertEqual(pin.angular_range, (0, 180))
            self.assertAlmostEqual(pin.location.position.X, 10)
        # Joints still connect parts.
        other = Box(5, 5, 5)
        j = RigidJoint("j", other, Location())
        cached.joints["mount"].connect_to(j)
        self.assertAlmostEqual(other.location.position.Z, 15)

        m.pair()
        p = m.pair()
        self.assertEqual(p.label, "pair")
        self.assertAlmostEqual(p.location.position.Z, 10)
        self.assertEqual([c.label for c in p.children], ["a", "b"])
        self.assertAlmostEqual(p.children[1].location.position.X, 5)
        self.assertEqual(tuple(p.children[1].color)[:3], (0, 0, 1))
        bb = p.bounding_box()
        self.assertAlmostEqual(bb.min.Z, 9)            # the cylinder
        self.assertAlmostEqual(bb.max.X, 6)

    def test_every_joint_type(self):
        src = '''
            from build123d import (Axis, BallJoint, Box, CylindricalJoint,
                                   LinearJoint, Location)
            from cadcache import part

            @part
            def block():
                b = Box(10, 10, 10)
                LinearJoint("slide", b, Axis((0, 0, 5), (1, 0, 0)),
                            linear_range=(-3, 3))
                CylindricalJoint("turn", b, Axis((0, 5, 0), (0, 1, 0)),
                                 linear_range=(0, 2), angular_range=(0, 90))
                BallJoint("ball", b, Location((5, 0, 0)),
                          angular_range=((-10, 10), (-20, 20), (0, 360)))
                return b
            '''
        m = self.module(src)
        m.block()
        b = m.block()
        self.assertEqual(self.kinds(), {"block": "cached"})
        self.assertEqual(b.joints["slide"].linear_range, (-3, 3))
        self.assertAlmostEqual(b.joints["slide"].location.position.Z, 5)
        self.assertEqual(b.joints["turn"].angular_range, (0, 90))
        self.assertAlmostEqual(b.joints["turn"].location.position.Y, 5)
        self.assertEqual(b.joints["ball"].angular_range[1], (-20, 20))
        self.assertAlmostEqual(b.joints["ball"].location.position.X, 5)
        # A joint on the loaded part still moves another part.
        other = Box(1, 1, 1)
        j = RigidJoint("j", other, Location())
        b.joints["slide"].connect_to(j, position=2)
        self.assertAlmostEqual(other.location.position.X, 2)
        self.assertAlmostEqual(other.location.position.Z, 5)

    def test_solid_stays_a_solid(self):
        m = self.module('''
            from build123d import Solid
            from cadcache import part

            @part
            def s():
                return Solid.make_box(1, 2, 3)
            ''')
        m.s()
        from build123d import Solid
        self.assertIs(type(m.s()), Solid)

    def test_shape_argument_from_a_part(self):
        src = PARTS + textwrap.dedent('''
            @part
            def drilled(body, d):
                return body - Cylinder(d / 2, 100)
            ''')
        m = self.module(src)
        m.drilled(m.block(10), 2)
        m.drilled(m.block(10), 2)
        self.assertEqual([e.kind for e in cadcache.events()],
                         ["built", "built", "cached", "cached"])
        # Moving the shape changes the key.
        moved = m.block(10).moved(Location((1, 0, 0)))
        self.assertNotEqual(m.drilled.key(moved, 2),
                            m.drilled.key(m.block(10), 2))


class Inputs(Project):
    def test_global_read_warns_and_is_keyed(self):
        src = '''
            from build123d import Box, MM
            from cadcache import part
            WIDTH = 10

            @part
            def box():
                return Box(WIDTH * MM, 1, 1)
            '''
        m = self.module(src)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always", cadcache.CacheWarning)
            m.box()
        msgs = [str(x.message) for x in w
                if issubclass(x.category, cadcache.GlobalReadWarning)]
        self.assertEqual(len(msgs), 1)                 # WIDTH, not MM
        self.assertIn("WIDTH", msgs[0])
        m.box()
        self.kinds()
        m = self.module(src.replace("WIDTH = 10", "WIDTH = 12"))
        self.assertAlmostEqual(m.box().volume, 12, places=6)
        self.assertEqual(self.kinds(), {"box": "built"})

    def test_file_read_warns_and_is_tracked(self):
        data = self.root / "size.txt"
        data.write_text("10")
        src = '''
            from pathlib import Path
            from build123d import Box
            from cadcache import part

            @part
            def box():
                w = float(Path(__file__).with_name("size.txt").read_text())
                return Box(w, 1, 1)
            '''
        m = self.module(src)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always", cadcache.CacheWarning)
            m.box()
        self.assertTrue(any(issubclass(x.category, cadcache.FileReadWarning)
                            and "size.txt" in str(x.message) for x in w))
        self.assertIn(str(data), cadcache.run_files())
        m.box()
        self.assertEqual(self.kinds(), {"box": "cached"})
        data.write_text("20")
        self.assertAlmostEqual(m.box().volume, 20, places=6)
        self.assertEqual(self.kinds(), {"box": "built"})

    def test_file_read_by_inner_part_rebuilds_outer(self):
        data = self.root / "size.txt"
        data.write_text("10")
        src = '''
            from pathlib import Path
            from build123d import Box, Cylinder
            from cadcache import part

            @part
            def inner():
                w = float(Path(__file__).with_name("size.txt").read_text())
                return Box(w, 1, 1)

            @part
            def outer():
                return inner() + Cylinder(1, 1)
            '''
        m = self.module(src)
        m.outer()
        m.outer()
        self.assertEqual(self.kinds(), {"outer": "cached", "inner": "built"})
        data.write_text("20")
        m.outer()
        self.assertEqual(self.kinds(), {"outer": "built", "inner": "built"})

    def test_path_argument_keyed_on_content(self):
        data = self.root / "size.txt"
        data.write_text("10")
        src = '''
            from build123d import Box
            from cadcache import part

            @part
            def box(path):
                return Box(float(path.read_text()), 1, 1)
            '''
        m = self.module(src)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always", cadcache.CacheWarning)
            k1 = m.box.key(data)
            m.box(data)
        self.assertEqual(w, [])                # it's an argument: no warning
        data.write_text("20")
        self.assertNotEqual(m.box.key(data), k1)

    def test_partnum_requirements(self):
        try:
            from partnum import load_requirements
        except ImportError:
            self.skipTest("partnum not installed")
        spec = self.root / "roller.yaml"
        text = ("scheme: pn/1\nclass: roller\nparameters:\n  d_mm: 30\n"
                "appearance: {colour: [1, 0, 0]}\n")
        spec.write_text(text)
        src = '''
            from build123d import Cylinder
            from cadcache import part

            @part
            def roller(req):
                return Cylinder(req["d_mm"] / 2, 4)
            '''
        m = self.module(src)
        k = m.roller.key(load_requirements(spec))
        self.assertEqual(k, m.roller.key(load_requirements(spec)))
        self.assertEqual(cadcache.requirements_key(spec),
                         cadcache.requirements_key(load_requirements(spec)))
        spec.write_text(text.replace("[1, 0, 0]", "[0, 1, 0]"))
        self.assertNotEqual(m.roller.key(load_requirements(spec)), k)
        spec.write_text(text.replace("30", "32"))
        self.assertNotEqual(m.roller.key(load_requirements(spec)), k)


class Hooks(Project):
    def test_salt_changes_every_key(self):
        m = self.module(PARTS)
        k = m.block.key(10)
        cadcache.configure(salt={"app": "1.0", "quality": "normal"})
        k2 = m.block.key(10)
        self.assertNotEqual(k, k2)
        self.assertEqual(m.block.key(10), k2)
        cadcache.configure(salt={"app": "1.1", "quality": "normal"})
        self.assertNotEqual(m.block.key(10), k2)
        with self.assertRaises(TypeError):
            cadcache.configure(salt=object())

    def test_before_save_mesh_is_kept_in_the_brep(self):
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.BRepTools import BRepTools
        seen = []

        def tessellate(shape):
            seen.append(shape)
            BRepMesh_IncrementalMesh(shape.wrapped, 0.5, True, 0.3, True)

        cadcache.configure(before_save=tessellate)
        m = self.module(PARTS)
        built = m.peg(4)
        cached = m.peg(4)
        self.assertEqual(len(seen), 1)           # only on a build
        for shape in (built, cached):
            self.assertTrue(BRepTools.Triangulation_s(shape.wrapped, 0.5))

    def test_attributes_are_kept(self):
        src = '''
            from build123d import Box, Compound, Location
            from cadcache import part

            @part
            def kit(n):
                kids = []
                for i in range(n):
                    b = Box(1, 1, 1).moved(Location((2 * i, 0, 0)))
                    b.bom = {"item": i, "tags": ["steel"]}
                    b.unkept = object()
                    kids.append(b)
                c = Compound(children=kids)
                c.bom = {"item": "kit"}
                return c
            '''
        cadcache.configure(attributes=["bom", "unkept"])
        m = self.module(src)
        m.kit(2)
        for c in (m.kit(2), m.kit(2)):
            self.assertEqual(c.bom, {"item": "kit"})
            self.assertEqual([k.bom for k in c.children],
                             [{"item": 0, "tags": ["steel"]},
                              {"item": 1, "tags": ["steel"]}])
            self.assertFalse(hasattr(c.children[0], "unkept"))
        self.assertEqual(self.kinds(), {"kit": "cached"})

    def test_old_sidecar_version_is_a_miss(self):
        m = self.module(PARTS)
        m.block(10)
        brep, side = m.block.paths(10)
        text = side.read_text(encoding="utf-8")
        side.write_text(text.replace('"sidecar": 2', '"sidecar": 1'),
                        encoding="utf-8")
        cadcache.reset()
        m.block(10)
        self.assertEqual(self.kinds(), {"block": "built"})


class NotCacheable(Project):
    SRC = '''
        from build123d import Box, Compound, Location
        import cadcache
        from cadcache import part

        registry = []

        @part
        def registered(w):
            b = Box(w, w, w)
            registry.append(b)
            cadcache.not_cacheable("registers itself")
            return b

        @part
        def plain(w):
            return Box(w, 1, 1)

        @part
        def outer(w):
            return Compound(children=[registered(w),
                                      plain(w).moved(Location((9, 0, 0)))])
        '''

    def test_result_is_returned_as_built_and_not_stored(self):
        m = self.module(self.SRC)
        a = m.registered(3)
        self.assertIs(a, m.registry[0])          # the object it built
        brep, side = m.registered.paths(3)
        self.assertFalse(side.exists())
        m.registered(3)
        self.assertEqual(len(m.registry), 2)     # the effect happens again
        events = cadcache.events()
        self.assertEqual([e.kind for e in events],
                         ["uncacheable", "uncacheable"])
        self.assertEqual(events[0].reason, "registers itself")
        self.assertRegex(events[0].part_number, PN)
        self.assertIsNone(events[0].entry)

    def test_outer_builds_are_not_stored_either(self):
        m = self.module(self.SRC)
        m.outer(2)
        self.assertEqual(self.kinds(), {"registered": "uncacheable",
                                        "plain": "built",
                                        "outer": "uncacheable"})
        m.outer(2)
        self.assertEqual(self.kinds(), {"registered": "uncacheable",
                                        "plain": "cached",
                                        "outer": "uncacheable"})
        self.assertEqual(len(m.registry), 2)

    def test_outside_a_build_it_does_nothing(self):
        cadcache.not_cacheable("nothing is building")


PN = r"^[A-HJKMNP-TV-Z]{3}-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{5}$"


class PartNumbers(Project):
    SRC = '''
        from build123d import Box, Compound, Location
        from cadcache import part

        THICKNESS = 2

        @part
        def plate(w, h=10):
            return Box(w, h, THICKNESS)

        @part(part_class="plate-pair")
        def pair(a, b):
            return Compound(children=[a, b.moved(Location((0, 0, 5)))])
        '''

    def test_format_and_inputs(self):
        m = self.module(self.SRC)
        n = m.plate.part_number(30)
        self.assertRegex(n, PN)
        self.assertEqual(n, m.plate.part_number(w=30.0, h=10))
        self.assertNotEqual(n, m.plate.part_number(31))
        self.assertNotEqual(n, m.plate.part_number(30, h=11))
        # The plain-data globals a part reads are inputs too.
        m2 = self.module(self.SRC.replace("THICKNESS = 2", "THICKNESS = 3"))
        self.assertNotEqual(m2.plate.part_number(30), n)

    def test_a_refactor_keeps_the_number_but_rebuilds(self):
        m = self.module(self.SRC)
        n, k = m.plate.part_number(30), m.plate.key(30)
        m2 = self.module(self.SRC.replace(
            "return Box(w, h, THICKNESS)",
            "size = (w, h, THICKNESS)\n            return Box(*size)"))
        self.assertEqual(m2.plate.part_number(30), n)
        self.assertNotEqual(m2.plate.key(30), k)

    def test_events_shapes_and_sidecars_carry_it(self):
        m = self.module(self.SRC)
        built = m.plate(30)
        cached = m.plate(30)
        n = m.plate.part_number(30)
        self.assertEqual([e.part_number for e in cadcache.events()], [n, n])
        self.assertEqual(cadcache.part_number_of(built), n)
        self.assertEqual(cadcache.part_number_of(cached), n)
        # An instance placed elsewhere is still that part; a boolean
        # result is a new shape.
        self.assertEqual(cadcache.part_number_of(
            cached.moved(Location((1, 0, 0)))), n)
        self.assertIsNone(cadcache.part_number_of(cached - Box(1, 1, 9)))
        self.assertIsNone(cadcache.part_number_of(Box(1, 1, 1)))
        brep, side = m.plate.paths(30)
        self.assertIn(n, side.read_text(encoding="utf-8"))
        self.assertTrue(cadcache.events()[0].entry.endswith(".brep"))

    def test_parts_passed_in_are_used_by_number(self):
        m = self.module(self.SRC)
        a, b = m.plate(30), m.plate(20)
        n = m.pair.part_number(a, b)
        self.assertEqual(n, m.pair.part_number(m.plate(30), m.plate(20)))
        self.assertNotEqual(n, m.pair.part_number(m.plate(30), m.plate(21)))
        self.assertNotEqual(n, m.pair.part_number(b, a))
        # A part that has been moved is still an input, at its place.
        moved = m.plate(20).moved(Location((1, 0, 0)))
        self.assertNotEqual(n, m.pair.part_number(a, moved))
        self.assertEqual(m.pair.part_class, "plate-pair")
        self.assertEqual(m.plate.part_class, "plate")

    def test_same_hash_as_partnum(self):
        try:
            import partnum
        except ImportError:
            self.skipTest("partnum not installed")
        from cadcache import partnums
        data = {"scheme": "pn/1", "class": "track-roller",
                "parameters": {"Track_W_mm": 18.50, "name": " a  b ",
                               "n": [1.0, 2, {"x": None, "y": True}]},
                "uses": {"screw": "QPT-8D2K-4MMXA"}}
        self.assertEqual(partnums.canonical(data), partnum.canonical(data))
        self.assertEqual(partnums.number(data), partnum.part_number(data))


class ExecutedScripts(Project):
    """App runners exec() a script in a namespace that isn't a module."""
    SRC = '''
        from build123d import Box
        from cadcache import part

        def size(w):
            return w * 2

        @part
        def block(w):
            return Box(size(w), 1, 1)

        result = block(3)
        '''

    def run_script(self, source):
        path = self.root / "design.py"
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        namespace = {"__name__": "__app_design__", "__file__": str(path)}
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"),
             namespace)
        return namespace

    def test_helpers_in_the_script_are_keyed(self):
        first = self.run_script(self.SRC)
        self.assertAlmostEqual(first["result"].volume, 6)
        self.run_script(self.SRC)
        self.assertEqual([e.kind for e in cadcache.events()],
                         ["built", "cached"])
        cadcache.reset()
        edited = self.run_script(self.SRC.replace("w * 2", "w * 3"))
        self.assertEqual(self.kinds(), {"block": "built"})
        self.assertAlmostEqual(edited["result"].volume, 9)

    def test_default_root_is_the_scripts_folder(self):
        cadcache.core.settings.root = None
        namespace = self.run_script(self.SRC)
        self.assertEqual(cadcache.keys.project_root(namespace["block"].fn),
                         self.root.resolve())


class Keys(unittest.TestCase):
    def test_canonical_values(self):
        c = keys.canon
        self.assertEqual(c(4), c(4.0))
        self.assertNotEqual(c(4), c("4"))
        self.assertNotEqual(keys.digest(c(True)), keys.digest(c(1)))
        self.assertEqual(c((1, 2)), c([1, 2]))
        self.assertEqual(c({"b": 1, "a": 2}), c({"a": 2, "b": 1}))
        self.assertEqual(c(Location((1, 2, 3), (0, 0, 90))),
                         c(Location((1.0, 2, 3), (0, 0, 90.0))))
        self.assertNotEqual(c(Location((1, 2, 3))), c(Location((1, 2, 4))))
        self.assertEqual(c(Color(1, 0, 0)), c(Color(1, 0, 0)))


class Watch(Project):
    def test_change_detection(self):
        f = self.root / "a.py"
        f.write_text("x = 1")
        before = watch.snapshot(watch.project_files(self.root))
        self.assertEqual(watch.changed(before), [])
        f.write_text("x = 22")
        self.assertEqual(watch.changed(before), [str(f)])

    def test_rerun_reloads_edited_modules(self):
        (self.root / "proj_helper.py").write_text("W = 2\n")
        script = self.root / "main.py"
        script.write_text(textwrap.dedent('''
            from build123d import Box
            from cadcache import part
            import proj_helper

            @part
            def box(w):
                return Box(w, 1, 1)

            RESULT.append(box(proj_helper.W).volume)
            '''))
        import builtins
        builtins.RESULT = []
        try:
            self.assertTrue(watch.run_once(script, root=self.root))
            self.assertTrue(watch.run_once(script, root=self.root))
            self.assertEqual(cadcache.summary(), "1 parts: 1 cached, 0 built")
            (self.root / "proj_helper.py").write_text("W = 3\n")
            self.assertTrue(watch.run_once(script, root=self.root))
            self.assertEqual([round(v, 6) for v in builtins.RESULT],
                             [2, 2, 3])
        finally:
            del builtins.RESULT


class Example(unittest.TestCase):
    def test_dincase_roller(self):
        try:
            import partnum  # noqa: F401
        except ImportError:
            self.skipTest("partnum not installed")
        tmp = tempfile.mkdtemp()
        cadcache.configure(cache_dir=tmp)
        sys.path.insert(0, str(EXAMPLES))
        try:
            ex = importlib.import_module("dincase_roller")
            from partnum import load_requirements
            req = load_requirements(EXAMPLES / "specs" / "slide-roller.yaml")
            r = ex.slide_roller(req)
            area = lambda d, b: math.pi / 4 * (d * d - b * b)
            self.assertAlmostEqual(
                r.volume, area(30, 8) * 4 + area(12, 8) * 0.5, places=3)
            bb = r.bounding_box()
            self.assertAlmostEqual(bb.size.X, 30)
            self.assertAlmostEqual(bb.size.Z, 4.5)
            self.assertAlmostEqual(bb.min.Z, 0)
            # The 8 mm bore goes right through.
            self.assertEqual(len(r.edges().filter_by(
                lambda e: e.geom_type.name == "CIRCLE"
                and abs(e.radius - 4) < 1e-6)), 2)
            asm = ex.main()
            self.assertEqual(len(asm.children), 2)
        finally:
            sys.path.remove(str(EXAMPLES))
            sys.modules.pop("dincase_roller", None)
            cadcache.core.settings.cache_dir = None
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
