"""Color-space helpers and numpy k-means color quantization.

This module is pure-numpy (no bpy) so it can be unit-tested standalone.
Everything the rest of the add-on does is anchored on *sRGB [0,1]* colors:
that is what the artist sees and what the 3MF ``displaycolor`` / ``m:color``
fields expect.  Clustering may happen in sRGB, scene-linear, or CIE-Lab space,
but the returned palette is always sRGB.
"""

from __future__ import annotations

import numpy as np

# Bambu Studio clusters imported colors down to at most 16 filaments; the AMS
# hardware tops out well below that.  Clamp K so we never emit something the
# slicer will silently collapse.
MAX_FILAMENTS = 16


# --------------------------------------------------------------------------- #
# Color space conversions (all operate on arrays in [0, 1], last axis = RGB)
# --------------------------------------------------------------------------- #
def srgb_to_linear(c):
    c = np.asarray(c, np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(c):
    c = np.clip(np.asarray(c, np.float64), 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * (c ** (1 / 2.4)) - 0.055)


def srgb_to_lab(rgb):
    """sRGB [0,1] -> CIE L*a*b* (D65).  Perceptually uniform-ish clustering."""
    lin = srgb_to_linear(rgb)
    m = np.array([[0.4124, 0.3576, 0.1805],
                  [0.2126, 0.7152, 0.0722],
                  [0.0193, 0.1192, 0.9505]])
    xyz = (lin @ m.T) / np.array([0.95047, 1.0, 1.08883])
    eps, kappa = 216 / 24389, 24389 / 27
    f = np.where(xyz > eps, np.cbrt(xyz), (kappa * xyz + 16) / 116)
    return np.stack([116 * f[..., 1] - 16,
                     500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])], axis=-1)


def to_working_space(srgb, space):
    space = (space or "LAB").upper()
    if space == "SRGB":
        return np.asarray(srgb, np.float64)
    if space == "LINEAR":
        return srgb_to_linear(srgb)
    return srgb_to_lab(srgb)  # LAB default


# --------------------------------------------------------------------------- #
# k-means (++ seeded), numpy only
# --------------------------------------------------------------------------- #
def _assign(x, c):
    """Return (labels, inertia) without building an N*K*3 temporary."""
    x2 = np.einsum("ij,ij->i", x, x)[:, None]
    c2 = np.einsum("ij,ij->i", c, c)[None, :]
    d2 = x2 + c2 - 2.0 * (x @ c.T)
    np.maximum(d2, 0.0, out=d2)
    lab = np.argmin(d2, axis=1)
    return lab, float(d2[np.arange(x.shape[0]), lab].sum())


def _update(x, lab, k, rng):
    counts = np.bincount(lab, minlength=k).astype(np.float64)
    c = np.empty((k, x.shape[1]))
    for d in range(x.shape[1]):
        c[:, d] = np.bincount(lab, weights=x[:, d], minlength=k)
    nz = counts > 0
    c[nz] /= counts[nz, None]
    if (~nz).any():  # reseed empty clusters from random points
        c[~nz] = x[rng.integers(0, x.shape[0], size=int((~nz).sum()))]
    return c


def _pp_init(x, k, rng):
    c = np.empty((k, x.shape[1]), x.dtype)
    c[0] = x[rng.integers(x.shape[0])]
    closest = np.sum((x - c[0]) ** 2, axis=1)
    for i in range(1, k):
        tot = closest.sum()
        if tot > 0:
            idx = rng.choice(x.shape[0], p=closest / tot)
        else:
            idx = rng.integers(x.shape[0])
        c[i] = x[idx]
        closest = np.minimum(closest, np.sum((x - c[i]) ** 2, axis=1))
    return c


def kmeans(x, k, n_iter=40, n_init=4, seed=0, tol=1e-7):
    """Return (centers (k,d), labels (n,)).  Best of ``n_init`` restarts."""
    x = np.asarray(x, np.float64)
    rng = np.random.default_rng(seed)
    best = (np.inf, None, None)
    k = max(1, min(int(k), x.shape[0]))
    for _ in range(n_init):
        c = _pp_init(x, k, rng)
        for _ in range(n_iter):
            lab, _ = _assign(x, c)
            new_c = _update(x, lab, k, rng)
            if np.sum((new_c - c) ** 2) < tol:
                c = new_c
                break
            c = new_c
        lab, inertia = _assign(x, c)
        if inertia < best[0]:
            best = (inertia, c, lab)
    return best[1], best[2]


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def quantize(samples_srgb, k, space="LAB", seed=0, max_fit=60000):
    """Quantize sRGB samples to ``k`` colors.

    Parameters
    ----------
    samples_srgb : (N, 3) array in sRGB [0,1]
    k            : requested number of filaments (clamped to MAX_FILAMENTS)
    space        : 'LAB' | 'LINEAR' | 'SRGB' clustering space
    max_fit      : if N is huge, fit the palette on a random subsample this
                   big, then assign all samples (keeps big meshes fast).

    Returns
    -------
    palette_srgb : (k, 3) sRGB palette, ordered by descending coverage
    labels       : (N,) index into palette_srgb for every input sample
    """
    samples_srgb = np.clip(np.asarray(samples_srgb, np.float64), 0.0, 1.0)
    n = samples_srgb.shape[0]
    k = max(1, min(int(k), MAX_FILAMENTS, n))

    x = to_working_space(samples_srgb, space)

    if n > max_fit:
        rng = np.random.default_rng(seed)
        fit = x[rng.choice(n, size=max_fit, replace=False)]
        centers, _ = kmeans(fit, k, seed=seed)
        labels, _ = _assign(x, centers)
    else:
        centers, labels = kmeans(x, k, seed=seed)

    k = centers.shape[0]
    # Drop clusters that no sample landed in (happens when K exceeds the number
    # of distinct colors) and remap labels contiguously, so we never ship a
    # placeholder/phantom filament. The emitted color count may be < K.
    used = np.unique(labels)
    remap_used = np.full(k, -1, dtype=np.int64)
    remap_used[used] = np.arange(len(used))
    labels = remap_used[labels]
    k = len(used)

    # Palette swatch = mean *sRGB* of each (now non-empty) cluster.
    palette = np.zeros((k, 3))
    for i in range(k):
        palette[i] = samples_srgb[labels == i].mean(axis=0)

    # Order palette by coverage (most-used filament first) and remap labels.
    counts = np.bincount(labels, minlength=k)
    order = np.argsort(-counts)
    remap = np.empty(k, dtype=np.int64)
    remap[order] = np.arange(k)
    palette = palette[order]
    labels = remap[labels]
    return np.clip(palette, 0.0, 1.0), labels.astype(np.int64)


def snap_to_palette(samples_srgb, palette_srgb, space="LAB"):
    """Assign each sample to its nearest existing palette entry (no new colors)."""
    x = to_working_space(np.clip(np.asarray(samples_srgb, np.float64), 0, 1), space)
    c = to_working_space(np.asarray(palette_srgb, np.float64), space)
    lab, _ = _assign(x, c)
    return lab.astype(np.int64)
