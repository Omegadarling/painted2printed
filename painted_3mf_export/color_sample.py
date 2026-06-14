"""Detect where a mesh's color lives (image texture vs. color attribute) and
sample one representative **sRGB** color per loop-triangle.

All public samplers return ``(n_tri, 3)`` float arrays in sRGB [0,1], aligned to
``mesh.loop_triangles`` order.  Call ``mesh.calc_loop_triangles()`` is done here.
"""

from __future__ import annotations

import numpy as np

from .quantize import linear_to_srgb


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def find_image_texture(material):
    """Return (Image, ShaderNodeTexImage) feeding Base Color, else first image
    node, else (None, None)."""
    if not material or not material.use_nodes or material.node_tree is None:
        return None, None
    nt = material.node_tree
    for n in nt.nodes:
        if n.bl_idname == "ShaderNodeBsdfPrincipled":
            bc = n.inputs.get("Base Color")
            if bc and bc.is_linked:
                src = bc.links[0].from_node
                if src.bl_idname == "ShaderNodeTexImage" and src.image is not None:
                    return src.image, src
    for n in nt.nodes:
        if n.bl_idname == "ShaderNodeTexImage" and n.image is not None:
            return n.image, n
    return None, None


def pick_color_attribute(mesh):
    """Return the most authoritative color attribute, or None.

    Prefers the render color attribute, then the active color attribute, then
    the first one.
    """
    cas = mesh.color_attributes
    if len(cas) == 0:
        return None
    try:
        if 0 <= cas.render_color_index < len(cas):
            return cas[cas.render_color_index]
    except Exception:
        pass
    return cas.active_color or cas[0]


def detect(obj, prefer="AUTO"):
    """Resolve the color source.

    Returns a dict: {kind: 'IMAGE'|'ATTRIBUTE'|'FLAT', image, node, attr,
    flat_srgb, summary}.
    """
    mesh = obj.data
    mat = obj.active_material
    img, node = find_image_texture(mat)
    has_uv = mesh.uv_layers.active is not None
    img_ok = img is not None and has_uv and getattr(img, "has_data", False)
    attr = pick_color_attribute(mesh)

    prefer = (prefer or "AUTO").upper()

    def use_image():
        return {"kind": "IMAGE", "image": img, "node": node, "attr": None,
                "flat_srgb": None,
                "summary": f"image '{img.name}' ({img.size[0]}x{img.size[1]}) via UV"}

    def use_attr():
        return {"kind": "ATTRIBUTE", "image": None, "node": None, "attr": attr,
                "flat_srgb": None,
                "summary": f"color attribute '{attr.name}' "
                           f"({attr.domain}/{attr.data_type})"}

    def use_flat():
        return {"kind": "FLAT", "image": None, "node": None, "attr": None,
                "flat_srgb": _flat_material_srgb(mat),
                "summary": "flat material base color (no texture/attribute found)"}

    if prefer == "IMAGE" and img_ok:
        return use_image()
    if prefer == "ATTRIBUTE" and attr is not None:
        return use_attr()
    # AUTO (and graceful fallback for forced modes that can't be satisfied)
    if img_ok:
        return use_image()
    if attr is not None:
        return use_attr()
    return use_flat()


def _flat_material_srgb(material):
    """A single sRGB color from a material with no texture/attribute."""
    if material and material.use_nodes and material.node_tree:
        for n in material.node_tree.nodes:
            if n.bl_idname == "ShaderNodeBsdfPrincipled":
                bc = n.inputs.get("Base Color")
                if bc is not None and not bc.is_linked:
                    return linear_to_srgb(np.array(bc.default_value[:3]))
    if material is not None:
        return linear_to_srgb(np.array(material.diffuse_color[:3]))
    return np.array([0.5, 0.5, 0.5])


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def _tri_loops(mesh):
    n_tri = len(mesh.loop_triangles)
    buf = np.empty(n_tri * 3, dtype=np.int64)
    mesh.loop_triangles.foreach_get("loops", buf)
    return buf.reshape(n_tri, 3)


# --------------------------------------------------------------------------- #
# Image sampling
# --------------------------------------------------------------------------- #
def image_to_srgb_numpy(image):
    """(h, w, 4) float32 in **sRGB**, row 0 = bottom (UV v=0).

    Blender's ``image.pixels`` returns the buffer values directly: linear for
    float buffers, and the stored (sRGB) bytes for 8-bit sRGB images.  We
    normalize everything to sRGB using the image's color space + bit depth.
    """
    if not getattr(image, "has_data", False) or tuple(image.size) == (0, 0):
        raise RuntimeError(f"image {image.name!r} has no loaded pixel data")
    w, h = image.size
    ch = image.channels
    flat = np.empty(w * h * ch, dtype=np.float32)
    image.pixels.foreach_get(flat)
    px = flat.reshape(h, w, ch).astype(np.float64)
    if ch < 4:
        pad = np.ones((h, w, 4 - ch))
        px = np.concatenate([px, pad], axis=2)

    cs = (image.colorspace_settings.name or "").lower()
    is_linear_buffer = bool(getattr(image, "is_float", False)) or \
        cs in ("non-color", "linear", "linear rec.709", "raw")
    if is_linear_buffer:
        px[..., :3] = linear_to_srgb(px[..., :3])
    return px  # sRGB, alpha untouched


def sample_uv(px, uvs, wrap="REPEAT"):
    """Nearest-texel sample.  uvs (N,2); returns (N,4). UV(0,0)=px[0,0]."""
    h, w = px.shape[:2]
    u = uvs[:, 0].astype(np.float64)
    v = uvs[:, 1].astype(np.float64)

    def fix(a):
        if wrap == "REPEAT":
            return np.mod(a, 1.0)
        if wrap == "MIRROR":
            return 1.0 - np.abs(np.mod(a, 2.0) - 1.0)
        return np.clip(a, 0.0, 1.0)

    u, v = fix(u), fix(v)
    x = np.clip((u * w).astype(int), 0, w - 1)
    y = np.clip((v * h).astype(int), 0, h - 1)  # no flip: row 0 == v=0
    return px[y, x]


def tri_colors_from_image(mesh, image, node):
    mesh.calc_loop_triangles()
    n_loops = len(mesh.loops)
    uv = np.empty(n_loops * 2, dtype=np.float32)
    mesh.uv_layers.active.uv.foreach_get("vector", uv)
    loop_uv = uv.reshape(n_loops, 2)
    centroid = loop_uv[_tri_loops(mesh)].mean(axis=1)
    px = image_to_srgb_numpy(image)
    wrap = getattr(node, "extension", "REPEAT") if node else "REPEAT"
    wrap = {"REPEAT": "REPEAT", "EXTEND": "EXTEND", "CLIP": "EXTEND",
            "MIRROR": "MIRROR"}.get(wrap, "REPEAT")
    return sample_uv(px, centroid, wrap)[:, :3]


# --------------------------------------------------------------------------- #
# Color-attribute sampling
# --------------------------------------------------------------------------- #
def tri_colors_from_attr(mesh, attr):
    """Per-triangle sRGB color (mean of the 3 corners).  Handles POINT/CORNER,
    FLOAT_COLOR/BYTE_COLOR.  Reads linear via foreach('color') then -> sRGB."""
    mesh.calc_loop_triangles()
    n = len(mesh.vertices) if attr.domain == "POINT" else len(mesh.loops)
    vals = np.empty(n * 4, dtype=np.float32)
    attr.data.foreach_get("color", vals)            # scene-linear
    vals = vals.reshape(n, 4)[:, :3]
    vals = linear_to_srgb(vals)                      # -> sRGB

    tri_loops = _tri_loops(mesh)
    if attr.domain == "CORNER":
        idx = tri_loops
    else:  # POINT: loop -> vertex
        lv = np.empty(len(mesh.loops), dtype=np.int64)
        mesh.loops.foreach_get("vertex_index", lv)
        idx = lv[tri_loops]
    return vals[idx].mean(axis=1)


# --------------------------------------------------------------------------- #
# Top level
# --------------------------------------------------------------------------- #
def tri_colors(obj, prefer="AUTO"):
    """Return (tri_srgb (n_tri,3), info dict from detect())."""
    mesh = obj.data
    mesh.calc_loop_triangles()
    info = detect(obj, prefer)
    if info["kind"] == "IMAGE":
        cols = tri_colors_from_image(mesh, info["image"], info["node"])
    elif info["kind"] == "ATTRIBUTE":
        cols = tri_colors_from_attr(mesh, info["attr"])
    else:  # FLAT
        cols = np.tile(info["flat_srgb"], (len(mesh.loop_triangles), 1))
    return np.clip(np.asarray(cols, np.float64), 0.0, 1.0), info
