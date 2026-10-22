# Panda Touch Desk Stand

<p align="center"><img src="images/mockup-hero.jpg" alt="Panda Touch Desk Stand (render)" width="560"></p>

An Echo-Show-style desk stand for the BIGTREETECH Panda Touch. It is built around BTT's own published CAD of the magnetic charging dock: [`3D/CAD/Magnetic Charging Dock and Mounting Bracket.STEP`](https://github.com/bigtreetech/PandaTouch/tree/master/3D).

## How it works

- **Dock fit.** The dock plate (133.31 × 88.31 mm) drops into a pocket with 0.2 mm clearance per side.
- **Mounting.** It is held by the plate's own 4 countersunk M3 holes. The stock printer bracket is not used.
- **Panda Touch.** It clicks onto the dock magnetically as usual. Its back 2.5 mm sits inside a frame lip with 0.5 mm per side.
- **Top-loading.** The frame is open at the top: lift the Panda off, drop it back in.
- **Tilt.** The screen leans back 20°.
- **Cable.** The dock's Type-C input is on its back and points down. The plug drops into a channel behind the dock's bottom notch, falls into the hollow body, and leaves through a small arch at the back bottom.
- **Side port.** The side USB-drive slot stays free.
- **Size and weight.** 140.3 × 90.4 × 92.5 mm (W × D × H). About 126 g of PLA.

## Files

| Folder | What |
|---|---|
| `print/` | Plate-ready Bambu Studio projects (P1S, 0.4 nozzle, PLA Basic, 0.20 Standard, **no supports**), plus STLs |
| `cad/` | STEP for each part. BTT's own dock CAD is in [their repository](https://github.com/bigtreetech/PandaTouch/tree/master/3D); it isn't included here |
| `images/` | Studio **mockup** renders. The Panda Touch in them is a stand-in shape, not a photo |
| `source/` | CadQuery source (`stand.py`, parametric) and the fit checks |

There are two variants. They are identical except for the screw holes:

- **inserts**: 4 × M3 heat-set inserts in Ø4.0 × 6 mm holes (standard short M3 inserts, up to 5.7 mm long).
- **direct screw**: M3 screws thread straight into Ø2.7 mm holes. No inserts needed.

## Print (P1S, as sliced)

| Part | Time | Filament |
|---|---|---|
| Stand (either variant) | about 2 h 42 min | 126 g PLA |
| Fit check plate (optional, print first) | about 41 min | 31 g |

- **Orientation.** Print upright, as loaded in the 3MF.
- **Supports.** None needed, because the bay is open at the top and the hollow inside is a self-supporting A-frame.
- **Premium finish.** Matte PLA (black or charcoal), 0.16 mm layers, 3–4 walls and ironing on the top.

## Hardware

- **Screws.** 4 × M3 × 10 countersunk (flat head) screws. The M3 × 6 screws in the Panda box are too short.
- **Inserts.** 4 × M3 heat-set inserts (inserts variant only).
- **Feet.** 4 × Ø10 mm × 1 mm silicone bumper feet, in the recesses under the base. Optional, but they make the stand feel planted.

## Assemble

1. **Inserts variant only.** Set the 4 heat-set inserts with a soldering iron.
2. **Cable.** Push the USB-C end of the charging cable in through the rear arch, up through the hollow and out of the opening behind the dock's notch. Plug it into the dock's Type-C port: it's on the back of the dock and faces down.
3. **Dock.** Lay the dock in its pocket so the plug drops into its channel. Screw in the 4 × M3 × 10.
4. **Panda Touch.** Set its power switch to **DC 5V**, then set it on the dock. The magnets centre it.

The power switch is on the Panda's back, behind the dock notch. To reach it, lift the Panda off.

## What was checked, and what wasn't

**Checked:**
- **Printed and in use.** The inserts variant has been printed and assembled with 4 × M3 heat-set inserts and 4 × M3 screws: the dock, the Panda Touch and the cable fit as designed.
- **Geometry against BTT's official dock STEP.** Zero overlap with the dock plate, its PCB/connector bar (which sticks out 3.1 mm behind the plate) and its screws.
- **Cable path.** Zero overlap with a 12.4 × 7 × 22 mm USB-C plug plus 4.2 mm cable at the measured port position.
- **Panda frame.** Zero overlap for Panda bodies of 132.0, 133.3 and 133.8 mm wide.
- **Mesh and slicing.** The meshes are watertight and passed Model Foundry's mesh check. The Bambu Studio slices re-open and re-slice.

**Not checked:**
- **Panda Touch body size.** BTT does not publish it. 132–133.3 × 87.7–88.3 × 15.5 mm was measured from the official manual drawing, using its magnet pattern as the scale. **Print the fit check plate first**: if the dock and the Panda seat in it, the stand will fit.
- **Cable plugs.** A very bulky USB-C overmold (wider than 14 mm or thicker than 7.5 mm) won't fit the channel. BTT's own cable is the intended one.

## Tweaks

All dimensions are parameters at the top of `source/stand.py`: `TILT`, `PANDA_CLEAR`, `PANDA_WRAP`, `CHIN`, `DEPTH_BOTTOM`, `BACK_BULGE` and the `PANDA_STAND_VARIANT` environment switch. Run `check_fit.py` again after changing them.

## License

The stand (STL, 3MF, STEP and source) is free for personal use under
[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/): print it, remix it and share
your remixes with credit, but don't sell the files or printed stands. PandaDeck's software is MIT.
If it's useful to you, a [tip](https://paypal.me/theboss3dfactory) is always appreciated.
