"""The export operator and the headless pipeline it drives."""

from __future__ import annotations

import os

import bpy
import numpy as np
from bpy.props import StringProperty
from bpy_extras.io_utils import ExportHelper

from . import color_sample, prep, quantize, threemf


class PipelineResult:
    def __init__(self):
        self.palette_srgb = None
        self.n_tris = 0
        self.n_verts = 0
        self.n_parts = 0
        self.warning = None
        self.source = ""
        self.analysis_before = None
        self.analysis_after = None
        self.messages = []


def run_pipeline(context, obj, settings, filepath):
    """Convert ``obj`` to a multicolor 3MF at ``filepath``.

    Operates entirely on a throwaway copy; the user's scene object is untouched.
    Returns a PipelineResult.  Raises on hard failure.
    """
    res = PipelineResult()

    # transform_apply / modifier_apply are OBJECT-mode operators; reassigning the
    # active object does not flush the global mode. Force OBJECT now (the user may
    # be in Vertex/Texture Paint, Sculpt, or Edit) and restore their mode after.
    prev_mode = None
    if context.object is not None and context.object.mode != "OBJECT":
        prev_mode = context.object.mode      # e.g. 'VERTEX_PAINT' (mode_set-compatible)
        bpy.ops.object.mode_set(mode="OBJECT")

    # ---- work copy (never mutate the user's object) ----------------------- #
    work = obj.copy()
    work.data = obj.data.copy()
    work.name = obj.name + "__p3mf_work"
    context.collection.objects.link(work)
    # Decouple from any parent so local transform == world transform (correct
    # scale-to-mm and geometry bake for parented meshes).
    work.matrix_world = obj.matrix_world.copy()
    work.parent = None
    work.matrix_parent_inverse.identity()
    prev_active = context.view_layer.objects.active
    prev_sel = [o for o in context.selected_objects]
    try:
        for o in prev_sel:
            o.select_set(False)
        work.select_set(True)
        context.view_layer.objects.active = work
        mesh = work.data

        # ensure triangulated topology for consistent sampling/export
        _triangulate(mesh)

        # ---- 1. sample per-face sRGB color ------------------------------- #
        tri_srgb, info = color_sample.tri_colors(work, settings.color_source)
        res.source = info["summary"]
        res.messages.append(f"Color source: {info['summary']}")

        # ---- 2. quantize to K filaments ---------------------------------- #
        palette, labels = quantize.quantize(
            tri_srgb, settings.num_filaments,
            space=settings.quant_space, seed=settings.seed)
        res.palette_srgb = palette
        res.messages.append(f"Quantized to {len(palette)} colors")

        # ---- 3. bake labels so they survive prep ------------------------- #
        prep.bake_labels(mesh, palette, labels)

        # ---- 4. optional prep (strict order) ----------------------------- #
        res.analysis_before = prep.analyze(mesh)
        if settings.do_validate:
            prep.repair(mesh)
            res.analysis_after = prep.analyze(mesh)
            res.messages.append("Repaired mesh")
        if settings.scale_to_mm:
            prep.scale_longest_to_mm(work, settings.target_size_mm)
            res.messages.append(f"Scaled longest side to {settings.target_size_mm} mm")
        else:
            prep.apply_transforms(work)
        if settings.do_remesh:
            prep.voxel_remesh_with_color(work, settings.voxel_size_mm)
            res.messages.append(f"Remeshed (voxel {settings.voxel_size_mm} mm)")

        # ---- 5. final labels from baked attribute ------------------------ #
        mesh = work.data
        _triangulate(mesh)
        final_labels = prep.read_labels(mesh, palette, space=settings.quant_space)
        verts, tris, keep = prep.extract_geometry(work)
        n_tri_full = len(keep)
        if final_labels is None or len(final_labels) != n_tri_full:
            # Only safe to reuse pre-prep labels when topology never changed.
            if len(labels) == n_tri_full:
                final_labels = labels
            else:
                raise RuntimeError(
                    "Per-triangle color labels were lost during prep (baked color "
                    "attribute missing after a topology change): "
                    f"{len(labels)} labels vs {n_tri_full} triangles. "
                    "Re-run without remesh, or check the data-transfer step.")
        final_labels = np.asarray(final_labels)[keep]   # align with area-filtered tris
        res.n_tris = len(tris)
        res.n_verts = len(verts)

        # ---- 6. write 3MF ------------------------------------------------ #
        title = os.path.splitext(os.path.basename(filepath))[0] or obj.name
        if settings.split_by_color:
            closed = prep.partition_by_label_closed(
                verts, tris, final_labels, len(palette),
                thickness=settings.shell_thickness_mm)
            parts_geom = [(p[0], p[1]) if p else None for p in closed]
            # An OPEN part (boundary holes) is what causes inside-out / inside-supports;
            # a few non-manifold pinch edges are slicer-tolerant, so only flag holes.
            open_parts = [k + 1 for k, p in enumerate(closed)
                          if p is not None and p[2]["boundary_edges"] > 0]
            nonman = sum(p[2]["non_manifold_edges"] for p in closed if p is not None)
            nbytes, n_parts = threemf.export_split(
                filepath, parts_geom, palette,
                title=title, write_basematerials=settings.write_basematerials)
            res.n_parts = n_parts
            res.messages.append(
                f"Wrote {os.path.basename(filepath)} as {n_parts} sealed, co-located "
                f"color parts ({len(palette)} colors)")
            if open_parts:
                res.warning = (f"{len(open_parts)} part(s) still have open holes "
                               f"(colors {open_parts}); they may slice oddly")
                res.messages.append(res.warning)
            elif nonman:
                res.messages.append(f"note: {nonman} non-manifold edge(s) across parts "
                                    "(minor; slicers auto-handle)")
        else:
            nbytes = threemf.export(
                filepath, verts, tris, final_labels, palette,
                title=title, write_basematerials=settings.write_basematerials)
            res.messages.append(f"Wrote {os.path.basename(filepath)} "
                                f"({res.n_tris} tris, {len(palette)} colors)")
        return res
    finally:
        prep.remove_bake_attr(work.data)
        wd = work.data
        bpy.data.objects.remove(work, do_unlink=True)
        if wd.users == 0:
            bpy.data.meshes.remove(wd)
        for o in prev_sel:
            try:
                o.select_set(True)
            except Exception:
                pass
        context.view_layer.objects.active = prev_active
        # Restore the artist's mode on their original object.
        if prev_mode is not None and context.view_layer.objects.active is not None:
            try:
                bpy.ops.object.mode_set(mode=prev_mode)
            except Exception:
                pass


def _triangulate(mesh):
    mesh.calc_loop_triangles()
    if all(len(p.vertices) == 3 for p in mesh.polygons):
        return
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.triangulate(bm, faces=bm.faces)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    mesh.calc_loop_triangles()


class EXPORT_OT_painted_3mf(bpy.types.Operator, ExportHelper):
    bl_idname = "export_mesh.painted_3mf"
    bl_label = "Export Painted 3MF"
    bl_description = "Convert the active painted mesh to a multicolor 3MF for AMS printing"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".3mf"
    filter_glob: StringProperty(default="*.3mf", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        ob = context.active_object
        return ob is not None and ob.type == "MESH"

    def execute(self, context):
        settings = context.scene.painted_3mf
        obj = context.active_object
        # ExportHelper only appends the extension via the file dialog; normalize
        # for programmatic callers passing a bare path too.
        if not self.filepath.lower().endswith(self.filename_ext.lower()):
            self.filepath += self.filename_ext
        others = [o for o in context.selected_objects
                  if o.type == "MESH" and o is not obj]
        if others:
            self.report({"WARNING"},
                        f"Exporting active object '{obj.name}' only; "
                        f"{len(others)} other selected mesh(es) ignored.")
        try:
            res = run_pipeline(context, obj, settings, self.filepath)
        except Exception as exc:  # surface a clean error, never a traceback dialog
            import traceback
            traceback.print_exc()
            self.report({"ERROR"}, f"3MF export failed: {exc}")
            return {"CANCELLED"}
        if res.warning:
            self.report({"WARNING"}, res.warning)
        extra = f", {res.n_parts} parts" if res.n_parts else ""
        self.report({"INFO"},
                    f"Exported {res.n_tris} tris, {len(res.palette_srgb)} colors"
                    f"{extra} ({res.source})")
        return {"FINISHED"}


CLASSES = (EXPORT_OT_painted_3mf,)
