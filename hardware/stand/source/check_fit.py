"""Fit checks against BIGTREETECH's official dock STEP plus proxies for the Type-C plug and the Panda Touch body."""
import math, sys
import cadquery as cq
sys.path.insert(0, sys.argv[1])
import stand

solid, plane = stand.build()
part = solid.val()
pose = stand.dock_pose(plane)

# official assembly re-oriented to dock local (dock_local.step origin = dock bbox min)
asm = cq.importers.importStep(sys.argv[2]).val()
dock_bb = max(asm.Solids(), key=lambda s: s.Volume()).BoundingBox()
shift = cq.Vector(-dock_bb.xmin, -dock_bb.ymin, -dock_bb.zmin)
keep = []
for s in asm.Solids():
    s = s.translate(shift)
    b = s.BoundingBox()
    if b.zmin < -5 or (s.Volume() < 60 and b.zmin < -0.5 and b.zmax > 4.9 and b.ymax < 25):
        continue  # stock mounting bracket and its two screws: replaced by the stand
    keep.append(s)
dock = cq.Compound.makeCompound(keep).moved(pose)


def overlap(a, b):
    try:
        return a.intersect(b).Volume()
    except Exception as exc:
        return f"error {type(exc).__name__}"


print("dock solids kept:", len(keep))
print("dock vs stand overlap mm3:", round(overlap(part, dock), 3))

# Type-C plug proxy: 12.4 x 7.0 overmold, 22 mm long below the mouth, cable 4.2 mm continuing 25 mm
plug = (cq.Workplane("XY").box(12.4, 22.0, 7.0).translate((stand.USB_X, stand.USB_MOUTH_Y - 11.0, -0.5))
        .union(cq.Workplane("XY").cylinder(25.0, 2.1, direct=(0, 1, 0)).translate((stand.USB_X, stand.USB_MOUTH_Y - 34.5, -0.5))))
print("plug+cable vs stand overlap mm3:", round(overlap(part, plug.val().moved(pose)), 3))

# Panda Touch body proxies: nominal (133.31 x 88.31) and drawing-scaled (132.0 x 87.7), 15.5 thick from dock z=5
for w, h in ((133.31, 88.31), (132.0, 87.7), (133.8, 88.8)):
    body = (cq.Workplane("XY").sketch().rect(w, h).vertices().fillet(6).finalize().extrude(15.5)
            .translate((stand.DOCK_CX, stand.DOCK_CY, 5.0)))
    print(f"panda {w}x{h} vs stand overlap mm3:", round(overlap(part, body.val().moved(pose)), 3))

bb = part.BoundingBox()
print("stand size mm:", round(bb.xlen, 1), round(bb.ylen, 1), round(bb.zlen, 1), "valid", part.isValid(),
      "volume cm3", round(part.Volume() / 1000, 1))
cq.exporters.export(cq.Compound.makeCompound([part, dock]), sys.argv[3] + "-with-dock.step")
cq.exporters.export(dock, sys.argv[3] + "-dock-posed.step")
cq.exporters.export(plug.val().moved(pose), sys.argv[3] + "-plug-posed.step")
cq.exporters.export(solid, sys.argv[3] + ".step")
cq.exporters.export(solid, sys.argv[3] + ".stl", tolerance=0.02, angularTolerance=0.15)
