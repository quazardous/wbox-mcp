"""
imaging.py — compare captures, with the parts that always change masked out.

A game's screen is never the same twice: a clock ticks, particles drift, the
water moves. Comparing two captures byte for byte says "different" every
time; comparing them with those zones masked says whether what matters
differs. The same mask serves every place pixels are compared: waiting for a
screen to settle, waiting for one that looks like a reference, a scenario's
checkpoints, and `compare` across runs.

A mask is a list of zones to ignore:

    {"rect": [x, y, w, h]}          pixels, or "12%" strings, any mix
    {"image": "masks/water.png"}    black (or transparent) = ignored
    {"only": [x, y, w, h]}          everything outside is ignored: for a
                                    screen whose background never holds
                                    still (a title over a video), compare
                                    the menu alone. Several "only" zones
                                    add up.

Captures are kept raw; masks apply when pixels are compared, so a mask can
change after the fact and old runs be compared again.

Needs numpy and Pillow: `pip install "wbox-mcp[scenario]"`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _deps():
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            f"comparing captures needs numpy and Pillow ({exc.name} is "
            "missing): pip install \"wbox-mcp[scenario]\"") from None
    return np, Image


# ── Masks ────────────────────────────────────────────────────────────


def _length(value, total: int, what: str) -> int:
    """A rect coordinate: pixels, or a percentage of the screen."""
    if isinstance(value, str) and value.strip().endswith("%"):
        try:
            return round(float(value.strip()[:-1]) / 100.0 * total)
        except ValueError:
            raise ValueError(f"mask {what}: {value!r} is not a percentage") from None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"mask {what}: {value!r} is neither pixels nor a percentage") from None


def resolve_mask(spec, named: dict | None = None) -> list[dict]:
    """A mask as written → its list of zones.

    `spec` is None/"none" (no mask), a name or a list of names from `named`,
    a list of zones, or a mix of names and zones in one list.
    """
    named = named or {}
    if spec in (None, "none", False, []):
        return []
    if isinstance(spec, (str, dict)):
        spec = [spec]
    zones = []
    for item in spec:
        if isinstance(item, str):
            if item not in named:
                raise ValueError(f"unknown mask {item!r}; defined: {sorted(named) or 'none'}")
            zones.extend(resolve_mask(named[item], {}))
        elif isinstance(item, dict) and ("rect" in item or "image" in item or "only" in item):
            zones.append(item)
        else:
            raise ValueError(f"mask zone {item!r}: expected {{rect: [x, y, w, h]}}, "
                             "{only: [x, y, w, h]}, {image: file.png} or a mask name")
    return zones


def _rect(r, width: int, height: int) -> tuple[int, int, int, int]:
    if not isinstance(r, (list, tuple)) or len(r) != 4:
        raise ValueError(f"mask rect {r!r}: expected [x, y, w, h]")
    return (_length(r[0], width, "x"), _length(r[1], height, "y"),
            _length(r[2], width, "w"), _length(r[3], height, "h"))


def mask_array(zones: list[dict], width: int, height: int, base_dir: Path | None = None):
    """A boolean array, True where pixels count (outside every zone)."""
    np, Image = _deps()
    keep = np.ones((height, width), dtype=bool)
    only = [z["only"] for z in zones if "only" in z]
    if only:
        inside = np.zeros((height, width), dtype=bool)
        for r in only:
            x, y, w, h = _rect(r, width, height)
            inside[max(y, 0):max(y + h, 0), max(x, 0):max(x + w, 0)] = True
        keep &= inside
    for zone in zones:
        if "only" in zone:
            continue
        if "rect" in zone:
            x, y, w, h = _rect(zone["rect"], width, height)
            keep[max(y, 0):max(y + h, 0), max(x, 0):max(x + w, 0)] = False
        else:
            path = Path(zone["image"])
            if base_dir is not None and not path.is_absolute():
                path = base_dir / path
            img = Image.open(path)
            if img.size != (width, height):
                img = img.resize((width, height), Image.NEAREST)
            if img.mode in ("RGBA", "LA") or "transparency" in img.info:
                alpha = np.asarray(img.convert("RGBA"))[:, :, 3]
                gray = np.asarray(img.convert("L"))
                ignored = (alpha < 128) | (gray < 128)
            else:
                ignored = np.asarray(img.convert("L")) < 128
            keep &= ~ignored
    return keep


def mask_digest(zones: list[dict], base_dir: Path | None = None) -> str:
    """A short hash of a mask, image contents included: two comparisons made
    with different masks must not pass for the same."""
    h = hashlib.sha256(json.dumps(zones, sort_keys=True).encode())
    for zone in zones:
        if "image" in zone:
            path = Path(zone["image"])
            if base_dir is not None and not path.is_absolute():
                path = base_dir / path
            try:
                h.update(path.read_bytes())
            except OSError:
                h.update(b"<missing>")
    return h.hexdigest()[:12]


# ── Comparing ────────────────────────────────────────────────────────


def load_rgb(path):
    np, Image = _deps()
    return np.asarray(Image.open(path).convert("RGB"))


def diff(a, b, zones: list[dict] | None = None, tolerance: int = 8,
         base_dir: Path | None = None) -> dict:
    """How much two captures differ, outside the mask.

    A pixel differs when one of its channels is more than `tolerance` apart
    (anti-aliasing and dithering move values by a few units between runs of
    the same frame). Returns the fraction of compared pixels that differ,
    their count, the mean absolute difference, and the compared area.
    `a` and `b` are paths or arrays.
    """
    np, _ = _deps()
    if not hasattr(a, "shape"):
        a = load_rgb(a)
    if not hasattr(b, "shape"):
        b = load_rgb(b)
    if a.shape != b.shape:
        return {"error": f"sizes differ: {a.shape[1]}x{a.shape[0]} vs "
                         f"{b.shape[1]}x{b.shape[0]}"}
    keep = mask_array(zones or [], a.shape[1], a.shape[0], base_dir)
    delta = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
    compared = int(keep.sum())
    differs = (delta > tolerance) & keep
    n = int(differs.sum())
    return {
        "differ": round(n / compared, 6) if compared else 0.0,
        "pixels": n,
        "compared": compared,
        "mean": round(float(delta[keep].mean()), 3) if compared else 0.0,
    }


def diff_image(a, b, out: Path, zones: list[dict] | None = None,
               tolerance: int = 8, base_dir: Path | None = None) -> Path:
    """A picture of what differs: the capture dimmed, differing pixels in
    red, masked zones hatched — to see what was ignored as well as what
    changed."""
    np, Image = _deps()
    if not hasattr(a, "shape"):
        a = load_rgb(a)
    if not hasattr(b, "shape"):
        b = load_rgb(b)
    keep = mask_array(zones or [], a.shape[1], a.shape[0], base_dir)
    delta = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
    view = (b.astype(np.float32) * 0.35).astype(np.uint8)
    view[(delta > tolerance) & keep] = (255, 40, 40)
    ys, xs = np.indices(keep.shape)
    hatch = ((xs + ys) // 6) % 2 == 0
    view[~keep & hatch] = (90, 90, 160)
    view[~keep & ~hatch] = (40, 40, 70)
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(view).save(out)
    return out


def varying_rects(paths: list, tolerance: int = 8, cell: int = 16,
                  zones: list[dict] | None = None, base_dir: Path | None = None) -> list[list[int]]:
    """Rectangles covering what changes between captures of the same moment.

    Taken from several runs at one checkpoint, what changes between them is
    what a mask should hide: a clock, particles. The screen is cut into
    `cell`-pixel cells; the varying ones are grouped into connected regions,
    each given as its bounding rectangle [x, y, w, h].
    """
    np, _ = _deps()
    imgs = [load_rgb(p) if not hasattr(p, "shape") else p for p in paths]
    if len(imgs) < 2:
        return []
    h, w = imgs[0].shape[:2]
    if any(im.shape != imgs[0].shape for im in imgs):
        raise ValueError("captures of different sizes")
    stack = np.stack([im.astype(np.int16) for im in imgs])
    spread = (stack.max(axis=0) - stack.min(axis=0)).max(axis=2) > tolerance
    spread &= mask_array(zones or [], w, h, base_dir)
    gh, gw = (h + cell - 1) // cell, (w + cell - 1) // cell
    padded = np.zeros((gh * cell, gw * cell), dtype=bool)
    padded[:h, :w] = spread
    grid = padded.reshape(gh, cell, gw, cell).any(axis=(1, 3))
    seen = np.zeros_like(grid)
    rects = []
    for gy in range(gh):
        for gx in range(gw):
            if not grid[gy, gx] or seen[gy, gx]:
                continue
            stack_ = [(gy, gx)]
            seen[gy, gx] = True
            y0 = y1 = gy
            x0 = x1 = gx
            while stack_:
                cy, cx = stack_.pop()
                y0, y1, x0, x1 = min(y0, cy), max(y1, cy), min(x0, cx), max(x1, cx)
                for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                    if 0 <= ny < gh and 0 <= nx < gw and grid[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack_.append((ny, nx))
            x, y = x0 * cell, y0 * cell
            rects.append([x, y, min((x1 + 1) * cell, w) - x, min((y1 + 1) * cell, h) - y])
    return rects
