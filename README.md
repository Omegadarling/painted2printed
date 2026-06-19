# Painted 3MF Export — Blender add-on

Converts a **painted 3D model** (image texture *or* vertex colors) into a
**multicolor `.3mf`** whose colors map to separate filaments / AMS slots in
**Bambu Studio** and **OrcaSlicer** (e.g. Bambu Lab H2C with AMS).

It reduces the model's colors to a chosen number of filaments (k‑means), assigns
each face one of those colors, runs optional print‑prep, and writes a 3MF.

---

## Install

1. In Blender 4.2+ / 5.x: **Edit ▸ Preferences ▸ Get Extensions ▸ ▾ ▸ Install from Disk…**
   and pick `painted_3mf_export-1.0.0.zip` (or just drag the zip into the Blender window).
2. Enable it if it isn't already.

Builds with no external dependencies (uses Blender's bundled numpy).

## Use

1. Select your painted mesh (make it the **active** object).
2. Open the **N‑panel** in the 3D Viewport → **"Paint→3MF"** tab
   (or use **File ▸ Export ▸ Painted Multicolor (.3mf)**).
3. Set the number of **Filaments (colors)** and any print‑prep options.
4. Click **Export 3MF** and choose a path.
5. Open the `.3mf` in Bambu Studio / OrcaSlicer → each color appears as its own
   filament; assign them to your AMS slots and slice.

You can export from Object **or** Vertex/Texture‑Paint/Sculpt/Edit mode — it
switches to Object mode internally and restores your mode afterward. Your scene
object is never modified (all work happens on a throwaway copy).

## Settings

| Setting | Meaning |
|---|---|
| **Color Source** | Auto‑detect (image texture → color attribute → flat material), or force one |
| **Filaments (colors)** | Target color count (1–16). Fewer are emitted if the model has fewer distinct colors |
| **Quantize In** | `Perceptual (Lab)` (best), `Linear`, or `sRGB` clustering space |
| **Seed** | k‑means determinism |
| **Validate / Repair Mesh** | Weld doubles, remove degenerates, fill holes, recalc normals (watertight) |
| **Scale to Size (mm)** | Scale longest dimension to the target mm |
| **Remesh for Color Detail** | Voxel‑remesh for crisper color borders (color is sampled *before* remesh and re‑transferred) |
| **Split Into Parts by Color** | Export each color as its own co‑located part instead of one painted mesh (see below) |
| **Write Fallback Materials** | Also emit `<basematerials>` for materials‑aware non‑Bambu slicers |

## How the colors reach Bambu (important)

Bambu Studio / OrcaSlicer's model loader (`bbs_3mf.cpp`) **ignores** the 3MF
core `<basematerials>` block. It only reads the **Materials‑extension color
group** — `<m:colorgroup>` / `<m:color>` with per‑triangle `pid` + `p1=p2=p3` —
mapping each unique color to its own filament. So this add‑on writes the colors
as an `m:colorgroup` (and keeps `<basematerials>` only as an inert,
non‑Bambu‑relevant extra). It also keeps the `Application` metadata **generic** —
setting it to `BambuStudio-*`/`OrcaSlicer-*` would switch the loader into native
project mode and **disable** this color‑import path.

## Split into parts by color

By default the export is **one painted mesh** — Bambu paints the surface and you
remap colors→filaments if needed. With **Split Into Parts by Color** enabled, the
3MF instead contains **one object made of N co-located parts**, one per color
(a 3MF assembly with a `<component>` per color at the identity transform).

In the slicer each part shows up separately, so you can **assign each part to any
filament / AMS slot by hand** — handy when your quantized colors don't match the
filaments actually loaded in the printer. All parts stay perfectly aligned (same
location), so together they reproduce the model.

Each part's open color-seam boundaries are **capped into a closed, watertight
shell**, so the slicer treats it as a real solid — supports go on the outside and
nothing renders inside-out. The parts are **hollow** (sealed outer shell with no
solid interior); set wall/infill per part in the slicer as usual. If a part's
seam can't be fully sealed it's reported as a warning rather than silently
exported broken.

> **Bambu Studio tip:** you may not need this at all. If you import the normal
> (non-split) export, Bambu Studio 2.5+ shows a **"Standard 3mf Import color"**
> dialog that lets you remap each model color to any loaded filament — same goal,
> no geometry split. Splitting is mainly useful for **OrcaSlicer** (which ignores
> per-triangle 3MF colors) or when you want separately re-colorable parts.

## Pipeline

```
work copy → triangulate → sample per‑face sRGB color (image UV / color attr / flat)
→ k‑means quantize to K → bake palette index into a CORNER color attribute
→ [validate/repair] → [scale to mm] → [voxel remesh + BVH color transfer]
→ re‑read labels (snap to palette) → drop degenerate faces
→ write m:colorgroup 3MF (atomic write)
```

## Limitations / notes

- Exports the **active** object only; other selected meshes are skipped (with a warning).
- Image‑texture path assumes the texture feeds Principled **Base Color** via the
  active UV map; sRGB vs. linear is handled per the image's color space/bit depth.
- Voxel remesh produces uniform, blocky color borders — use it only when the
  source mesh is too low‑poly for clean color edges; it lowers detail otherwise.
- Color count emitted can be **less than** requested if the model has fewer
  distinct colors (no phantom filler colors).

## Files

`__init__.py` (register) · `props.py` (settings) · `ui.py` (panel/menu) ·
`operator.py` (pipeline + operator) · `color_sample.py` (detect/sample) ·
`quantize.py` (k‑means + color spaces) · `prep.py` (repair/scale/remesh) ·
`threemf.py` (3MF writer).
