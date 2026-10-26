"""
Dincase slide roller and its sleeve, in build123d with cadcache.

The specs in specs/ are copies of RolaCase's specs/slide-roller.yaml and
slide-sleeve.yaml (RolaCase is the authority). Each part takes its partnum
requirements as an argument, so a change to the file rebuilds that part
and nothing else.

    python examples/dincase_roller.py           # build, or load from cache
    cadcache watch examples/dincase_roller.py   # and again on every save

Roller: axis +Z, hub from z = 0 (it bears on the shelf rail), rim above.
Like build_slide.py's SolidWorks roller, bed_chamfer_mm isn't modelled.
"""
from pathlib import Path

from build123d import (Align, Color, Compound, Cylinder, Location, Part,
                       RigidJoint)
from partnum import load_requirements

from cadcache import assembly, part

SPECS = Path(__file__).parent / "specs"


def tube(od, bore, length, z=0):
    """A tube on the Z axis from z up."""
    up = (Align.CENTER, Align.CENTER, Align.MIN)
    return Location((0, 0, z)) * (Cylinder(od / 2, length, align=up) -
                                  Cylinder(bore / 2, length, align=up))


def colour(req):
    return Color(*req.get("appearance", {}).get("colour", (0.7, 0.7, 0.7)))


@part
def slide_roller(req):
    hub_h, rim_h = req["hub_h_mm"], req["roller_h_mm"]
    roller = Part() + [tube(req["hub_d_mm"], req["bore_d_mm"], hub_h),
                       tube(req["roller_d_mm"], req["bore_d_mm"], rim_h,
                            z=hub_h)]
    roller.label, roller.color = "slide roller " + req.id, colour(req)
    RigidJoint("axle", roller, Location())     # bore centre, hub face
    return roller


@part
def roller_sleeve(req, roller_req):
    od = roller_req["bore_d_mm"] - req["running_fit_mm"]
    length = roller_req["hub_h_mm"] + roller_req["roller_h_mm"]
    sleeve = Part() + tube(od, req["bore_d_mm"], length)
    sleeve.label, sleeve.color = "roller sleeve " + req.id, colour(req)
    RigidJoint("axle", sleeve, Location())     # bore centre, flush end
    return sleeve


@assembly
def roller_on_sleeve(roller, sleeve):
    sleeve.joints["axle"].connect_to(roller.joints["axle"])
    return Compound(children=[sleeve, roller], label="slide roller")


def main():
    roller_req = load_requirements(SPECS / "slide-roller.yaml")
    sleeve_req = load_requirements(SPECS / "slide-sleeve.yaml")
    assert sleeve_req.use("roller").id == roller_req.id
    asm = roller_on_sleeve(slide_roller(roller_req),
                           roller_sleeve(sleeve_req, roller_req))
    for p in asm.children:
        print("%-36s %8.1f mm^3" % (p.label, p.volume))
    try:
        from ocp_vscode import show
    except ImportError:
        return asm
    show(asm)
    return asm


if __name__ == "__main__":
    main()
