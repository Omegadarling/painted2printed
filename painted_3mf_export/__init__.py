"""Painted 3MF Export — Blender extension.

Converts a painted mesh (image texture or vertex colors) into a multicolor 3MF
whose colors map to separate filaments / AMS slots in Bambu Studio & OrcaSlicer.

Metadata lives in blender_manifest.toml (no bl_info — this is an extension).
"""

import bpy

from . import operator, props, ui

_CLASSES = (*props.CLASSES, *operator.CLASSES, *ui.CLASSES)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.painted_3mf = bpy.props.PointerProperty(type=props.Painted3MFSettings)
    bpy.types.TOPBAR_MT_file_export.append(ui.menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(ui.menu_func_export)
    if hasattr(bpy.types.Scene, "painted_3mf"):
        del bpy.types.Scene.painted_3mf
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
