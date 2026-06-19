"""User-facing settings, stored on the Scene as ``scene.painted_3mf``."""

from __future__ import annotations

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty
from bpy.types import PropertyGroup

from .quantize import MAX_FILAMENTS


class Painted3MFSettings(PropertyGroup):
    color_source: EnumProperty(
        name="Color Source",
        description="Where to read the model's colors from",
        items=[
            ("AUTO", "Auto-detect", "Image texture if present, else color attribute, else flat material color"),
            ("IMAGE", "Image Texture", "Sample the Base Color image through the active UV map"),
            ("ATTRIBUTE", "Color Attribute", "Use vertex/face color attributes (vertex paint)"),
        ],
        default="AUTO",
    )
    num_filaments: IntProperty(
        name="Filaments (colors)",
        description="Number of colors to reduce the model to (one per AMS slot)",
        default=4, min=1, soft_max=MAX_FILAMENTS, max=MAX_FILAMENTS,
    )
    quant_space: EnumProperty(
        name="Quantize In",
        description="Color space used to cluster colors",
        items=[
            ("LAB", "Perceptual (Lab)", "CIE Lab — best visual color grouping"),
            ("LINEAR", "Linear", "Cluster in scene-linear RGB"),
            ("SRGB", "sRGB", "Cluster in raw sRGB values"),
        ],
        default="LAB",
    )
    seed: IntProperty(
        name="Seed", description="Random seed for k-means (determinism)",
        default=0, min=0,
    )

    do_validate: BoolProperty(
        name="Validate / Repair Mesh",
        description="Weld doubles, remove degenerate faces, fill holes, recalc normals",
        default=True,
    )
    scale_to_mm: BoolProperty(
        name="Scale to Size (mm)",
        description="Scale so the longest dimension equals the target size in millimeters",
        default=True,
    )
    target_size_mm: FloatProperty(
        name="Longest Side (mm)", description="Target size of the longest dimension",
        default=50.0, min=0.1, soft_max=300.0,
    )
    do_remesh: BoolProperty(
        name="Remesh for Color Detail",
        description="Voxel-remesh to add triangle density so color borders print crisply "
                    "(color is sampled BEFORE remesh and carried across)",
        default=False,
    )
    voxel_size_mm: FloatProperty(
        name="Voxel Size (mm)",
        description="Smaller = sharper color borders + much higher poly count",
        default=0.5, min=0.01, soft_max=5.0,
    )

    split_by_color: BoolProperty(
        name="Split Into Parts by Color",
        description="Export each color as its own closed (hollow), co-located part — one object "
                    "with N watertight parts — instead of a single painted mesh. Each part is "
                    "independently filament-assignable in Bambu Studio AND OrcaSlicer. "
                    "Tip: for Bambu Studio you can instead leave this OFF and remap each color "
                    "to a loaded filament in Bambu's import-color dialog",
        default=False,
    )
    shell_thickness_mm: FloatProperty(
        name="Part Wall (mm)",
        description="Wall thickness used to seal each split part into a closed hollow shell "
                    "(parts are sealed so the slicer supports them correctly)",
        default=1.0, min=0.1, soft_max=5.0,
    )
    write_basematerials: BoolProperty(
        name="Write Fallback Materials",
        description="Also emit <basematerials> for materials-extension-aware non-Bambu "
                    "slicers (ignored by Bambu; not used by core-only viewers)",
        default=True,
    )


CLASSES = (Painted3MFSettings,)
