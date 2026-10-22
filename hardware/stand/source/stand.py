"""Panda Touch desk stand (Echo-Show style), built around BIGTREETECH's official charging-dock CAD.

v4: top-loading U cradle (no overhangs, prints upright without supports), bowed back, hollow A-frame inside
open to the bed, cable from the dock's Type-C port down through the notch into the hollow and out a rear arch.

Frames
- World: X width (centred), Y depth (+ = back), Z up; the screen faces -Y.
- Dock local (official STEP, re-oriented): x 0..133.31, y 0..88.31 (y = 0 = notch edge = bottom),
  z = 0 rear rib plane, z = 5 front face (Panda side).
- Bay local: origin at the bay floor centre; x = X, y = up the tilted face, z = out of the face.
  Bay floor = dock rear plane (z = 0); dock front face z = 5; face plane z = BAY_DEPTH.
"""
import math
import cadquery as cq

# --- official dock geometry (measured from "Magnetic Charging Dock and Mounting Bracket.STEP") -----------------
DOCK_W, DOCK_H, DOCK_T = 133.31, 88.31, 5.0
DOCK_R = 6.0
DOCK_CX, DOCK_CY = DOCK_W / 2, DOCK_H / 2
SCREWS_TOP = [(30.65, 44.15), (102.65, 44.15)]       # countersunk M3, boss underside at z = 0
SCREWS_BOTTOM = [(32.70, 16.15), (100.65, 16.15)]    # boss underside at z = 1.4 (recessed zone)
RECESS_ZONE = (22.8, 110.5, 0.6, 30.3)               # zone recessed to z = 1.4 (stock bracket sat here)
NOTCH = (47.57, 85.74, 0.0, 25.24)
PCB_BAR = (28.95, 104.35, 52.92, 62.92, -3.1)        # x0, x1, y0, y1, rear-most z (sticks out behind the plate)
USB_X, USB_MOUTH_Y = 79.25, 52.9                     # Type-C receptacle centre x; mouth faces -y (down)

# --- Panda Touch (official manual drawing scaled by its magnet pattern: ~132-133.3 x 87.7-88.3 x 15.5) ------
PANDA_WRAP = 2.5          # rear 2.5 mm only: clears the side USB-drive port (3-12.3 mm from a face) without slots

# --- stand parameters ---------------------------------------------------------------------------------------
TILT = math.radians(20)
DOCK_CLEAR = 0.20
PANDA_CLEAR = 0.50
WALL = 3.0
CHIN = 10.0
BAY_DEPTH = DOCK_T + PANDA_WRAP                       # 11.0
DEPTH_BOTTOM = 92.0
TOP_THICK = 20.0
BACK_BULGE = 9.0
SHELL = 3.2                                           # hollow-body wall
KEEP_SOLID = 23.0                                     # solid material behind the face (bay + pockets + inserts)
import os
# Variant: "inserts" = M3 heat-set inserts (hole 4.0 x 6.0); "direct" = M3 screws tap straight into a 2.7 mm hole.
VARIANT = os.environ.get("PANDA_STAND_VARIANT", "inserts")
INSERT_D, INSERT_DEPTH, SCREW_CLEAR_D = (4.0, 6.0, 3.2) if VARIANT == "inserts" else (2.7, 9.0, 2.7)
FOOT_D, FOOT_DEPTH = 10.6, 1.2
EXIT_W, EXIT_H = 16.0, 11.0                           # rear cable arch

panda_w = max(DOCK_W, 133.3) + 2 * PANDA_CLEAR        # 134.31
panda_h = max(DOCK_H, 88.3) + 2 * PANDA_CLEAR         # 89.31
WIDTH = panda_w + 2 * WALL
FACE_LEN = CHIN + panda_h                             # the Panda's top edge sits flush with the stand's top

u = (0.0, math.sin(TILT), math.cos(TILT))
n_out = (0.0, -math.cos(TILT), math.sin(TILT))


def _v(*parts):
    return tuple(sum(p[i] for p in parts) for i in range(3))


def _s(k, vec):
    return tuple(k * c for c in vec)


def profile_points():
    top = _s(FACE_LEN, u)
    crown = _v(top, _s(-TOP_THICK, n_out))
    rear = (0.0, DEPTH_BOTTOM, 0.0)
    mid = ((crown[1] + rear[1]) / 2, (crown[2] + rear[2]) / 2)
    chord = (rear[1] - crown[1], rear[2] - crown[2]); L = math.hypot(*chord)
    bulge = (mid[0] + BACK_BULGE * -chord[1] / L, mid[1] + BACK_BULGE * chord[0] / L)
    return (top[1], top[2]), (crown[1], crown[2]), bulge, (rear[1], rear[2])


def _profile(wp):
    top, crown, bulge, rear = profile_points()
    return wp.moveTo(0, 0).lineTo(*top).lineTo(*crown).threePointArc(bulge, rear).close()


def body():
    top, crown, _, rear = profile_points()
    solid = _profile(cq.Workplane("YZ")).extrude(WIDTH / 2, both=True)
    pick = lambda p: cq.selectors.NearestToPointSelector((0, p[0], p[1]))
    solid = solid.edges("|X").edges(pick(crown)).fillet(13)
    solid = solid.edges("|X").edges(pick(rear)).fillet(4)
    # One R3 pass for the front edges and every side edge: equal radii blend into clean (watertight) corners.
    front = solid.edges("|X").edges(pick(top)).vals() + solid.edges("|X").edges(pick((0, 0))).vals()
    return solid.newObject(front + solid.edges("not |X").vals()).fillet(3)


def hollow():
    """A-frame void open to the bed: front wall parallel to the face (20 deg), back wall follows the bowed back."""
    inner = _profile(cq.Workplane("YZ")).offset2D(-SHELL).extrude(WIDTH / 2 - SHELL, both=True)
    ring = inner.intersect(cq.Workplane("XY").box(400, 400, 0.4).translate((0, 0, SHELL + 0.4))).val().BoundingBox()
    base = cq.Workplane("XY").box(WIDTH - 2 * SHELL, ring.ymax - ring.ymin, SHELL + 2.0).translate(
        (0, (ring.ymin + ring.ymax) / 2, (SHELL + 2.0) / 2 - 1.0))
    face = [(0.0, 0.0), _s(200, u)[1:], _v(_s(200, u), _s(-KEEP_SOLID, n_out))[1:], _s(-KEEP_SOLID, n_out)[1:]]
    keep = cq.Workplane("YZ").polyline(face).close().extrude(WIDTH, both=True)
    return inner.union(base).cut(keep)


def bay_plane():
    centre_on_face = _s(CHIN + panda_h / 2, u)
    floor = _v(centre_on_face, _s(-BAY_DEPTH, n_out))
    return cq.Plane(origin=floor, xDir=(1, 0, 0), normal=n_out)


def _rrect_up(plane, w, h, r, z0, z1, extra=60.0):
    """Rounded at the bottom corners only; continues up and out through the top (top-loading cradle)."""
    lower = (cq.Workplane(plane).workplane(offset=z0).sketch().rect(w, h).vertices().fillet(r).finalize()
             .extrude(z1 - z0))
    return lower.union(_box(plane, -w / 2, w / 2, 0.0, h / 2 + extra, z0, z1))


def _box(plane, x0, x1, y0, y1, z0, z1):
    return (cq.Workplane(plane).workplane(offset=z0).center((x0 + x1) / 2, (y0 + y1) / 2)
            .rect(x1 - x0, y1 - y0).extrude(z1 - z0))


def d2b(x, y):
    return x - DOCK_CX, y - DOCK_CY


def pockets(plane):
    return [_rrect_up(plane, DOCK_W + 2 * DOCK_CLEAR, DOCK_H + 2 * DOCK_CLEAR, DOCK_R + DOCK_CLEAR, 0.0, DOCK_T + 0.01),
            _rrect_up(plane, panda_w, panda_h, DOCK_R + PANDA_CLEAR, DOCK_T, BAY_DEPTH + 40.0)]


def detail_cuts(plane):
    parts = []
    x0, y0 = d2b(PCB_BAR[0] - 0.5, PCB_BAR[2] - 0.5)
    x1, y1 = d2b(PCB_BAR[1] + 0.5, PCB_BAR[3] + 0.5)
    parts.append(_box(plane, x0, x1, y0, y1, PCB_BAR[4] - 0.5, 2.0))
    # Type-C plug channel straight down from the receptacle, then the cable drop behind the notch into the hollow
    x0, _ = d2b(USB_X - 7.25, 0)
    x1, y1 = d2b(USB_X + 7.25, USB_MOUTH_Y + 0.6)
    _, y0 = d2b(0, -1.0)
    parts.append(_box(plane, x0, x1, y0, y1, -5.0, 2.0))
    nx0, _ = d2b(NOTCH[0] + 8.0, 0)
    _, ny1 = d2b(0, NOTCH[3] + 3.0)
    _, ny0 = d2b(0, 2.0)
    parts.append(_box(plane, nx0, x1, ny0, ny1, -(KEEP_SOLID - BAY_DEPTH) - 3.0, 2.0))
    for (x, y), top in [(p, 0.0) for p in SCREWS_TOP] + [(p, 1.3) for p in SCREWS_BOTTOM]:
        bx, by = d2b(x, y)
        parts.append(cq.Workplane(plane).workplane(offset=top - INSERT_DEPTH).center(bx, by)
                     .circle(INSERT_D / 2).extrude(INSERT_DEPTH + 0.5))
        parts.append(cq.Workplane(plane).workplane(offset=top - INSERT_DEPTH - 3.0).center(bx, by)
                     .circle(SCREW_CLEAR_D / 2).extrude(3.1))
    return parts


def plateau(plane):
    x0, y0 = d2b(RECESS_ZONE[0] + 0.5, RECESS_ZONE[2] + 0.5)
    x1, y1 = d2b(RECESS_ZONE[1] - 0.5, RECESS_ZONE[3] - 1.0)
    pad = _box(plane, x0, x1, y0, y1, 0.0, 1.3)
    nx0, ny0 = d2b(NOTCH[0] - 0.5, -1.0)
    nx1, ny1 = d2b(NOTCH[1] + 0.5, NOTCH[3] + 0.5)
    return pad.cut(_box(plane, nx0, nx1, ny0, ny1, -1.0, 3.0))


def lead_in(plane, c=0.8):
    return (cq.Workplane(plane).workplane(offset=BAY_DEPTH + 0.01)
            .sketch().rect(panda_w + 2 * c, panda_h + 2 * c).vertices().fillet(DOCK_R + PANDA_CLEAR + c).finalize()
            .extrude(-c, taper=45))


def rear_exit():
    """Small arch through the back wall at the bottom for the cable."""
    x, r = USB_X - DOCK_CX, EXIT_W / 2
    y0, y1 = DEPTH_BOTTOM - 30.0, DEPTH_BOTTOM + 5.0
    box = cq.Workplane("XY").box(EXIT_W, y1 - y0, EXIT_H - r + 1.0).translate((x, (y0 + y1) / 2, (EXIT_H - r - 1.0) / 2))
    top = cq.Workplane("XZ").cylinder(y1 - y0, r).translate((x, (y0 + y1) / 2, EXIT_H - r))
    return box.union(top)


def feet_pads_and_holes():
    pads, holes = None, None
    spots = [(sx * (WIDTH / 2 - 9.5), y) for sx in (-1, 1) for y in (9.0, DEPTH_BOTTOM - 14.0)]  # rear pads fuse with the side walls
    for x, y in spots:
        p = cq.Workplane("XY").center(x, y).circle(FOOT_D / 2 + 2.2).extrude(5.0)
        h = cq.Workplane("XY").workplane(offset=-1).center(x, y).circle(FOOT_D / 2).extrude(1 + FOOT_DEPTH)
        pads = p if pads is None else pads.union(p)
        holes = h if holes is None else holes.union(h)
    return pads, holes


def build():
    plane = bay_plane()
    solid = body()
    void = hollow()
    pads, holes = feet_pads_and_holes()
    solid = solid.cut(void.cut(pads))
    for c in pockets(plane):
        solid = solid.cut(c)
    solid = solid.union(plateau(plane))
    for c in detail_cuts(plane):
        solid = solid.cut(c)
    solid = solid.cut(lead_in(plane)).cut(rear_exit()).cut(holes)
    return solid, plane


def dock_pose(plane):
    origin = plane.toWorldCoords((-DOCK_CX, -DOCK_CY, 0.0))
    return cq.Location(cq.Plane(origin=origin.toTuple(), xDir=(1, 0, 0), normal=n_out))
