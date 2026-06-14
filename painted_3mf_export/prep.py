"""Print-prep: mesh validation/repair, scale-to-mm, voxel remesh, and baking /
carrying the quantized color through topology changes.

The quantized per-face palette index is baked into a CORNER FLOAT_COLOR
attribute (BAKE_ATTR) *before* any topology-changing step, so that after
repair/remesh we can re-read it and re-snap to the same palette.  This keeps the
exported colors consistent no matter what prep the user enables.
"""

from __future__ import annotations

import bmesh
import bpy
import numpy as np
from mathutils.bvhtree import BVHTree

from .quantize import linear_to_srgb, srgb_to_linear

BAKE_ATTR = "_p3mf_quantized"


# --------------------------------------------------------------------------- #
# Inspection / repair
# --------------------------------------------------------------------------- #
def analyze(mesh):
    bm = bmesh.new()
    bm.from_mesh(mesh)
    info = {
        "verts": len(bm.verts),
        "faces": len(bm.faces),
        "non_manifold_edges": sum(1 for e in bm.edges if not e.is_manifold),
        "boundary_edges": sum(1 for e in bm.edges if e.is_boundary),
        "degenerate_faces": sum(1 for f in bm.faces if f.calc_area() < 1e-8),
        "loose_verts": sum(1 for v in bm.verts if not v.link_edges),
    }
    info["watertight"] = (info["non_manifold_edges"] == 0
                          and info["boundary_edges"] == 0)
    bm.free()
    return info


def repair(mesh, merge_dist=1e-5):
    """In-place bmesh cleanup: weld doubles, kill degenerates, fill holes,
    drop loose geometry, recalc normals outward.  Preserves attribute layers."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=merge_dist)
    bmesh.ops.dissolve_degenerate(bm, dist=1e-6, edges=bm.edges)
    loose = [v for v in bm.verts if not v.link_edges]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
    bmesh.ops.holes_fill(bm, edges=bm.edges, sides=0)
    bmesh.ops.dissolve_degenerate(bm, dist=1e-6, edges=bm.edges)  # re-clean fill slivers
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()


# --------------------------------------------------------------------------- #
# Scale
# --------------------------------------------------------------------------- #
def scale_longest_to_mm(obj, target_mm):
    """Uniformly scale so the longest world-space dimension == target_mm,
    then bake all transforms (1 Blender unit == 1 mm in the 3MF)."""
    bpy.context.view_layer.objects.active = obj
    dims = max(obj.dimensions)
    if dims > 1e-9:
        obj.scale = obj.scale * (target_mm / dims)
    bpy.context.view_layer.update()
    apply_transforms(obj)


def apply_transforms(obj):
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)


# --------------------------------------------------------------------------- #
# Bake / read the quantized color attribute
# --------------------------------------------------------------------------- #
def bake_labels(mesh, palette_srgb, tri_labels):
    """Write a CORNER FLOAT_COLOR attribute encoding the per-triangle palette
    index, so it survives remove_doubles / remesh / data-transfer."""
    mesh.calc_loop_triangles()
    n_loops = len(mesh.loops)
    pal_lin = srgb_to_linear(np.asarray(palette_srgb))      # store linear
    loop_rgba = np.empty((n_loops, 4), dtype=np.float32)
    loop_rgba[:, :3] = 0.5
    loop_rgba[:, 3] = 1.0

    n_tri = len(mesh.loop_triangles)
    tl = np.empty(n_tri * 3, dtype=np.int64)
    mesh.loop_triangles.foreach_get("loops", tl)
    loop_rgba[tl.reshape(-1), :3] = pal_lin[np.repeat(tri_labels, 3)]

    if BAKE_ATTR in mesh.color_attributes:
        mesh.color_attributes.remove(mesh.color_attributes[BAKE_ATTR])
    attr = mesh.color_attributes.new(BAKE_ATTR, "FLOAT_COLOR", "CORNER")
    attr.data.foreach_set("color", loop_rgba.reshape(-1))
    mesh.update()


def read_labels(mesh, palette_srgb, space="LAB"):
    """Re-derive per-triangle palette indices from the baked attribute, snapping
    to the nearest existing palette color (introduces no new colors)."""
    from .color_sample import tri_colors_from_attr
    from .quantize import snap_to_palette
    attr = mesh.color_attributes.get(BAKE_ATTR)
    if attr is None:
        return None
    tri_srgb = tri_colors_from_attr(mesh, attr)
    return snap_to_palette(tri_srgb, palette_srgb, space=space)


def remove_bake_attr(mesh):
    a = mesh.color_attributes.get(BAKE_ATTR)
    if a is not None:
        mesh.color_attributes.remove(a)


# --------------------------------------------------------------------------- #
# Remesh
# --------------------------------------------------------------------------- #
def _face_colors_from_corner_attr(mesh, attr):
    """Per-polygon mean of corner colors -> (n_poly, 3) plus (loop_start, loop_total)."""
    n_loops = len(mesh.loops)
    cols = np.empty(n_loops * 4, dtype=np.float32)
    attr.data.foreach_get("color", cols)
    cols = cols.reshape(n_loops, 4)[:, :3]
    n_poly = len(mesh.polygons)
    ls = np.empty(n_poly, dtype=np.int64)
    lt = np.empty(n_poly, dtype=np.int64)
    mesh.polygons.foreach_get("loop_start", ls)
    mesh.polygons.foreach_get("loop_total", lt)
    out = np.empty((n_poly, 3))
    for i in range(n_poly):
        a = ls[i]
        out[i] = cols[a:a + lt[i]].mean(axis=0)
    return out, ls, lt


def voxel_remesh_with_color(obj, voxel_size):
    """Voxel-remesh ``obj`` for crisper color borders. Voxel remesh wipes all
    attributes, so we snapshot the pre-remesh baked face colors + a BVH tree,
    remesh, then re-bake BAKE_ATTR by nearest-source-face lookup. This is far more
    reliable than a DATA_TRANSFER modifier (which silently failed to carry the
    custom color layer across)."""
    src_mesh = obj.data
    attr = src_mesh.color_attributes.get(BAKE_ATTR)
    src_face_col = None
    bvh = None
    if attr is not None:
        src_face_col, _, _ = _face_colors_from_corner_attr(src_mesh, attr)
        nv = len(src_mesh.vertices)
        co = np.empty(nv * 3, dtype=np.float32)
        src_mesh.vertices.foreach_get("co", co)
        verts = co.reshape(nv, 3).tolist()
        polys = [tuple(p.vertices) for p in src_mesh.polygons]
        bvh = BVHTree.FromPolygons(verts, polys, all_triangles=False)

    bpy.context.view_layer.objects.active = obj
    m = obj.modifiers.new("Remesh", "REMESH")
    m.mode = "VOXEL"
    m.voxel_size = max(voxel_size, 1e-4)
    m.adaptivity = 0.0
    bpy.ops.object.modifier_apply(modifier=m.name)

    me = obj.data
    if bvh is not None:
        n_poly = len(me.polygons)
        face_col = np.full((n_poly, 3), 0.5)
        for i, p in enumerate(me.polygons):
            _loc, _nor, idx, _d = bvh.find_nearest(p.center)
            if idx is not None and idx < len(src_face_col):
                face_col[i] = src_face_col[idx]
        if BAKE_ATTR in me.color_attributes:
            me.color_attributes.remove(me.color_attributes[BAKE_ATTR])
        na = me.color_attributes.new(BAKE_ATTR, "FLOAT_COLOR", "CORNER")
        loop_rgba = np.ones((len(me.loops), 4), dtype=np.float32)
        ls = np.empty(n_poly, dtype=np.int64)
        lt = np.empty(n_poly, dtype=np.int64)
        me.polygons.foreach_get("loop_start", ls)
        me.polygons.foreach_get("loop_total", lt)
        for i in range(n_poly):
            a = ls[i]
            loop_rgba[a:a + lt[i], :3] = face_col[i]
        na.data.foreach_set("color", loop_rgba.reshape(-1))
    me.update()


# --------------------------------------------------------------------------- #
# Final geometry extraction (world space)
# --------------------------------------------------------------------------- #
def extract_geometry(obj):
    """Return (verts (V,3) world-space float64, tris (T,3) int64, keep (n_tri,) bool).

    ``keep`` is the per-loop-triangle mask of non-degenerate faces actually kept
    in ``tris`` — the caller must apply it to the per-triangle label array so the
    two stay aligned.
    """
    mesh = obj.data
    mesh.calc_loop_triangles()
    n_v = len(mesh.vertices)
    co = np.empty(n_v * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(n_v, 3)
    mw = np.array(obj.matrix_world, dtype=np.float64)
    world = (np.column_stack([co, np.ones(n_v)]) @ mw.T)[:, :3]

    n_tri = len(mesh.loop_triangles)
    tv = np.empty(n_tri * 3, dtype=np.int64)
    mesh.loop_triangles.foreach_get("vertices", tv)
    tris = tv.reshape(n_tri, 3)

    # Drop zero/near-zero-area triangles (a 3MF SHOULD have non-degenerate faces).
    e1 = world[tris[:, 1]] - world[tris[:, 0]]
    e2 = world[tris[:, 2]] - world[tris[:, 0]]
    keep = np.linalg.norm(np.cross(e1, e2), axis=1) > 1e-9
    return world, tris[keep], keep
