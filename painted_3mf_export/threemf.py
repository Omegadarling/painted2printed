"""Write a multicolor 3MF.

Encoding (verified against BambuStudio/OrcaSlicer ``bbs_3mf.cpp`` and the 3MF
Core + Materials specs):

* ``<m:colorgroup>`` / ``<m:color>`` is the LOAD-BEARING path: Bambu/Orca map
  each unique ``m:color`` to its own filament / AMS slot.  Triangles reference it
  with ``pid`` + ``p1=p2=p3`` (equal indices = flat face).
* ``<basematerials>`` is emitted only for materials-extension-aware non-Bambu
  slicers that read base materials directly.  Because the file sets
  ``requiredextensions="m"`` (color is bound through the m: colorgroup),
  core-only/"generic" viewers MUST refuse the file and never see it.  It is a
  distinct, currently UNREFERENCED resource id (object/triangles use the
  colorgroup pid), so it is inert for most consumers and ignored by Bambu.
* The ``Application`` metadata is deliberately a GENERIC string.  Setting it to
  ``BambuStudio-*`` / ``OrcaSlicer-*`` flips the loader into "native project"
  mode and DISABLES the colorgroup color import.

The XML is built with string templates (not ElementTree) for guaranteed
namespace declarations and to stay fast on meshes with >100k triangles.
"""

from __future__ import annotations

import os
import tempfile
import zipfile

import numpy as np

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
MAT_NS = "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"
APPLICATION = "Painted3MFExport-1.1.0"   # generic on purpose; do NOT use "BambuStudio-"

# Resource ids share one namespace; keep distinct.
CG_ID, BM_ID, OBJ_ID = 1, 2, 3
PART_ID_BASE = 10          # split-mode child object ids: 10, 11, 12, ...
IDENTITY = "1 0 0 0 1 0 0 0 1 0 0 0"

CONTENT_TYPES = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    b'<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    b'</Types>'
)

RELS = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    b'<Relationship Id="rel0" Target="/3D/3dmodel.model" '
    b'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
    b'</Relationships>'
)


def srgb_to_hex(rgb, alpha=None):
    rgb = np.nan_to_num(np.asarray(rgb, float)[:3], nan=0.5, posinf=1.0, neginf=0.0)
    s = np.clip(np.round(rgb * 255), 0, 255).astype(int)
    h = "#%02X%02X%02X" % (int(s[0]), int(s[1]), int(s[2]))
    if alpha is not None:
        h += "%02X" % int(round(min(max(alpha, 0.0), 1.0) * 255))
    return h


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _verts_block(verts):
    return ('<vertices>' + "".join(
        '<vertex x="%.6g" y="%.6g" z="%.6g"/>' % (v[0], v[1], v[2]) for v in verts)
        + '</vertices>')


def _tris_block(tris, labels):
    """``labels`` is an int (uniform color for every triangle) or a per-triangle array."""
    if np.isscalar(labels):
        lab = int(labels)
        body = "".join(
            '<triangle v1="%d" v2="%d" v3="%d" pid="%d" p1="%d" p2="%d" p3="%d"/>'
            % (t[0], t[1], t[2], CG_ID, lab, lab, lab) for t in tris)
    else:
        body = "".join(
            '<triangle v1="%d" v2="%d" v3="%d" pid="%d" p1="%d" p2="%d" p3="%d"/>'
            % (t[0], t[1], t[2], CG_ID, lab, lab, lab) for t, lab in zip(tris, labels))
    return '<triangles>' + body + '</triangles>'


def _colors_block(out, palette_srgb, write_basematerials):
    out.append(f'<m:colorgroup id="{CG_ID}">')
    out.extend(f'<m:color color="{srgb_to_hex(c)}"/>' for c in palette_srgb)
    out.append('</m:colorgroup>')
    if write_basematerials:
        out.append(f'<basematerials id="{BM_ID}">')
        out.extend(f'<base name="Filament {i + 1}" displaycolor="{srgb_to_hex(c, 1.0)}"/>'
                   for i, c in enumerate(palette_srgb))
        out.append('</basematerials>')


def build_model_xml(verts, tris, tri_labels, palette_srgb,
                    title="painted_model", write_basematerials=True):
    """Return the 3dmodel.model document as bytes."""
    verts = np.asarray(verts, float)
    tris = np.asarray(tris, np.int64)
    tri_labels = np.asarray(tri_labels, np.int64)
    palette_srgb = np.asarray(palette_srgb, float)

    if len(tri_labels) != len(tris):
        raise ValueError(f"tri_labels ({len(tri_labels)}) != tris ({len(tris)})")
    if len(verts) and not np.isfinite(verts).all():
        raise ValueError("Non-finite vertex coordinate(s) in mesh; cannot write valid 3MF")

    out = ['<?xml version="1.0" encoding="UTF-8"?>\n',
           '<model unit="millimeter" xml:lang="en-US" '
           f'xmlns="{CORE_NS}" xmlns:m="{MAT_NS}" requiredextensions="m">',
           f'<metadata name="Application">{APPLICATION}</metadata>',
           f'<metadata name="Title">{_esc(title)}</metadata>',
           '<resources>']

    # Load-bearing color group (Bambu/Orca AMS) + inert basematerials fallback.
    _colors_block(out, palette_srgb, write_basematerials)

    # Single object: default property -> colorgroup index 0; triangles override.
    out.append(f'<object id="{OBJ_ID}" type="model" pid="{CG_ID}" pindex="0"><mesh>')
    out.append(_verts_block(verts))
    out.append(_tris_block(tris, tri_labels))
    out.append('</mesh></object>')
    out.append('</resources>')
    out.append('<build>'
               f'<item objectid="{OBJ_ID}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
               '</build>')
    out.append('</model>')
    return "".join(out).encode("utf-8")


def build_model_xml_split(parts, palette_srgb, title="painted_model",
                          write_basematerials=True):
    """Build a 3MF where each color is its OWN co-located part.

    ``parts`` is a list indexed by color k -> (verts, tris) of just that color's
    geometry, or None for colors with no triangles. Each non-empty color becomes a
    child <object> (object-level + per-triangle color k), and all children are
    grouped under a single assembly <object> via <component>s at the identity
    transform. The slicer therefore shows ONE object made of N parts, all at the
    same location, each independently re-assignable to any filament/AMS slot.
    """
    palette_srgb = np.asarray(palette_srgb, float)
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n',
           '<model unit="millimeter" xml:lang="en-US" '
           f'xmlns="{CORE_NS}" xmlns:m="{MAT_NS}" requiredextensions="m">',
           f'<metadata name="Application">{APPLICATION}</metadata>',
           f'<metadata name="Title">{_esc(title)}</metadata>',
           '<resources>']
    _colors_block(out, palette_srgb, write_basematerials)

    child_ids = []
    for k, part in enumerate(parts):
        if part is None:
            continue
        verts, tris = np.asarray(part[0], float), np.asarray(part[1], np.int64)
        if len(verts) and not np.isfinite(verts).all():
            raise ValueError("Non-finite vertex coordinate(s); cannot write valid 3MF")
        oid = PART_ID_BASE + k
        child_ids.append(oid)
        out.append(f'<object id="{oid}" type="model" name="Color {k + 1}" '
                   f'pid="{CG_ID}" pindex="{k}"><mesh>')
        out.append(_verts_block(verts))
        out.append(_tris_block(tris, k))
        out.append('</mesh></object>')

    if not child_ids:
        raise ValueError("No non-empty color parts to export")

    # Assembly object: co-locate every part at the identity transform.
    out.append(f'<object id="{OBJ_ID}" type="model"><components>')
    out.extend(f'<component objectid="{cid}" transform="{IDENTITY}"/>' for cid in child_ids)
    out.append('</components></object>')
    out.append('</resources>')
    out.append(f'<build><item objectid="{OBJ_ID}" transform="{IDENTITY}"/></build>')
    out.append('</model>')
    return "".join(out).encode("utf-8")


def write_3mf(filepath, model_xml):
    # Atomic write: build a temp file in the same directory, then os.replace, so
    # an I/O failure (e.g. a Dropbox folder hiccup) never clobbers a good export.
    directory = os.path.dirname(os.path.abspath(filepath)) or "."
    fd, tmp = tempfile.mkstemp(suffix=".3mf.tmp", dir=directory)
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", CONTENT_TYPES)
            z.writestr("_rels/.rels", RELS)
            z.writestr("3D/3dmodel.model", model_xml)
        os.replace(tmp, filepath)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def export(filepath, verts, tris, tri_labels, palette_srgb,
           title="painted_model", write_basematerials=True):
    xml = build_model_xml(verts, tris, tri_labels, palette_srgb,
                          title=title, write_basematerials=write_basematerials)
    write_3mf(filepath, xml)
    return len(xml)


def export_split(filepath, parts, palette_srgb,
                 title="painted_model", write_basematerials=True):
    xml = build_model_xml_split(parts, palette_srgb,
                                title=title, write_basematerials=write_basematerials)
    write_3mf(filepath, xml)
    n_parts = sum(1 for p in parts if p is not None)
    return len(xml), n_parts
