"""Sidebar panel (View3D > N > Paint→3MF) and File > Export menu entry."""

from __future__ import annotations

import bpy

from . import color_sample
from .operator import EXPORT_OT_painted_3mf


def _detect_summary(obj):
    """Cheap, draw-safe readout (no pixel reads)."""
    if obj is None or obj.type != "MESH":
        return "No mesh selected"
    mesh = obj.data
    bits = []
    img, _ = color_sample.find_image_texture(obj.active_material)
    has_uv = mesh.uv_layers.active is not None
    if img is not None:
        bits.append(f"image '{img.name}'" + ("" if has_uv else " (NO UV!)"))
    n_ca = len(mesh.color_attributes)
    if n_ca:
        bits.append(f"{n_ca} color attr" + ("s" if n_ca > 1 else ""))
    if not bits:
        bits.append("flat material color only")
    return ", ".join(bits)


class VIEW3D_PT_painted_3mf(bpy.types.Panel):
    bl_label = "Painted → 3MF"
    bl_idname = "VIEW3D_PT_painted_3mf"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Paint→3MF"

    def draw(self, context):
        layout = self.layout
        s = context.scene.painted_3mf
        obj = context.active_object

        box = layout.box()
        box.label(text="Detected:", icon="VIEWZOOM")
        box.label(text=_detect_summary(obj))

        col = layout.column(align=True)
        col.prop(s, "color_source")
        col.prop(s, "num_filaments")
        col.prop(s, "quant_space")
        col.prop(s, "seed")

        layout.separator()
        layout.label(text="Print Prep:", icon="MODIFIER")
        col = layout.column(align=True)
        col.prop(s, "do_validate")
        col.prop(s, "scale_to_mm")
        sub = col.column(align=True)
        sub.enabled = s.scale_to_mm
        sub.prop(s, "target_size_mm")
        col.prop(s, "do_remesh")
        sub = col.column(align=True)
        sub.enabled = s.do_remesh
        sub.prop(s, "voxel_size_mm")

        layout.separator()
        layout.label(text="Output:", icon="FILE_3D")
        col = layout.column(align=True)
        col.prop(s, "split_by_color")
        if s.split_by_color:
            sub = col.column(align=True)
            sub.prop(s, "shell_thickness_mm")
        else:
            hint = col.column(align=True)
            hint.scale_y = 0.7
            hint.label(text="Single object: remap colors in", icon="INFO")
            hint.label(text="Bambu's import-color dialog.")
        col.prop(s, "write_basematerials")

        layout.separator()
        row = layout.row()
        row.scale_y = 1.5
        row.enabled = obj is not None and obj.type == "MESH"
        row.operator(EXPORT_OT_painted_3mf.bl_idname, text="Export 3MF", icon="EXPORT")


def menu_func_export(self, context):
    self.layout.operator(EXPORT_OT_painted_3mf.bl_idname, text="Painted Multicolor (.3mf)")


CLASSES = (VIEW3D_PT_painted_3mf,)
