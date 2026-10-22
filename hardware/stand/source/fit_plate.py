"""Fit-check plate: the stand's bay (dock pocket, Panda lip, 4 screw holes) cut out as a thin flat print."""
import sys
import cadquery as cq
sys.path.insert(0, sys.argv[1])
import stand

solid, plane = stand.build()
FLOOR = 1.6
# keep only the slab of the stand between 1.6 mm behind the bay floor and the face, within the frame outline
slab = (cq.Workplane(plane).workplane(offset=-FLOOR)
        .center(0, 0).rect(stand.WIDTH + 2, stand.panda_h + 2 * stand.CHIN + 10).extrude(FLOOR + stand.BAY_DEPTH + 0.01))
plate = solid.intersect(slab)
# lay it flat: bay-local frame -> world, floor underside on the bed
flat = plate.val().moved(cq.Location(plane).inverse).translate((0, 0, FLOOR))
bb = flat.BoundingBox()
print("plate", round(bb.xlen, 1), round(bb.ylen, 1), round(bb.zlen, 2), "volume cm3", round(flat.Volume() / 1000, 1), "valid", flat.isValid())
cq.exporters.export(flat, sys.argv[2] + ".step")
cq.exporters.export(flat, sys.argv[2] + ".stl", tolerance=0.03, angularTolerance=0.08)
