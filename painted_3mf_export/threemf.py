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
APPLICATION = "Painted3MFExport-1.0.0"   # generic on purpose; do NOT use "BambuStudio-"

# Resource ids share one namespace; keep distinct.
CG_ID, BM_ID, OBJ_ID = 1, 2, 3

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

    # Load-bearing color group (Bambu/Orca AMS).
    out.append(f'<m:colorgroup id="{CG_ID}">')
    out.extend(f'<m:color color="{srgb_to_hex(c)}"/>' for c in palette_srgb)
    out.append('</m:colorgroup>')

    # Base materials block (unreferenced; only meaningful to materials-extension-
    # aware non-Bambu slicers). NOT a fallback for core-only viewers, which must
    # refuse the file due to requiredextensions="m".
    if write_basematerials:
        out.append(f'<basematerials id="{BM_ID}">')
        out.extend(
            f'<base name="Filament {i + 1}" displaycolor="{srgb_to_hex(c, 1.0)}"/>'
            for i, c in enumerate(palette_srgb))
        out.append('</basematerials>')

    # Object: default property -> colorgroup index 0; triangles override.
    out.append(f'<object id="{OBJ_ID}" type="model" pid="{CG_ID}" pindex="0"><mesh>')

    out.append('<vertices>')
    out.append("".join(
        '<vertex x="%.6g" y="%.6g" z="%.6g"/>' % (v[0], v[1], v[2])
        for v in verts))
    out.append('</vertices>')

    out.append('<triangles>')
    out.append("".join(
        '<triangle v1="%d" v2="%d" v3="%d" pid="%d" p1="%d" p2="%d" p3="%d"/>'
        % (t[0], t[1], t[2], CG_ID, L, L, L)
        for t, L in zip(tris, tri_labels)))
    out.append('</triangles>')

    out.append('</mesh></object>')
    out.append('</resources>')
    out.append('<build>'
               f'<item objectid="{OBJ_ID}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
               '</build>')
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
