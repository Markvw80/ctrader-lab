"""Small dependency-free SVG charts (self-contained reports, readable offline).

Colors come from CSS variables defined in the page (light/dark mode aware).
"""

import math
from datetime import UTC, datetime
from html import escape

PALETTE = ["var(--c1)", "var(--c2)", "var(--c3)", "var(--c4)", "var(--c5)", "var(--c6)"]


def _downsample(points: list[tuple[float, float]], max_points: int = 1500) -> list[tuple[float, float]]:
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    out = [points[int(i * step)] for i in range(max_points)]
    out[-1] = points[-1]
    return out


def _nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if (hi - lo) / step <= n:
            break
    start = (lo // step) * step
    ticks, v = [], start
    while v <= hi + step * 1e-9:
        if v >= lo - step * 1e-9:
            ticks.append(round(v, 10))
        v += step
    return ticks


def _fmt(v: float) -> str:
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 10:
        return f"{v:.0f}"
    return f"{v:.2f}".rstrip("0").rstrip(".")


def line_chart(series: list[tuple[str, list[tuple[datetime, float]]]], height: int = 260,
               y_suffix: str = "", fill_negative: bool = False, title: str = "") -> str:
    """Time series lines. series: [(label, [(datetime, value), ...]), ...]."""
    width, ml, mr, mt, mb = 900, 64, 16, 24, 28
    pts = [(lbl, _downsample([(d.timestamp(), v) for d, v in data])) for lbl, data in series if data]
    if not pts:
        return '<p class="muted">no data</p>'
    xs = [x for _, p in pts for x, _ in p]
    ys = [y for _, p in pts for _, y in p]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if fill_negative:
        y1 = max(y1, 0)
    if y0 == y1:
        y0, y1 = y0 - 1, y1 + 1
    pad = (y1 - y0) * 0.05
    y0, y1 = y0 - (0 if fill_negative and y1 == 0 else pad), y1 + (0 if fill_negative else pad)
    sx = lambda x: ml + (x - x0) / ((x1 - x0) or 1) * (width - ml - mr)
    sy = lambda y: mt + (y1 - y) / (y1 - y0) * (height - mt - mb)

    parts = [(f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
             f'aria-label="{escape(title or "chart")}">')]
    for t in _nice_ticks(y0, y1):
        y = sy(t)
        parts.append(f'<line x1="{ml}" x2="{width - mr}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{ml - 6}" y="{y + 4:.1f}" class="tick" text-anchor="end">'
                     f'{_fmt(t)}{y_suffix}</text>')
    for i in range(5):
        x = x0 + (x1 - x0) * i / 4
        label = datetime.fromtimestamp(x, UTC).strftime("%Y-%m-%d")
        anchor = "start" if i == 0 else "end" if i == 4 else "middle"
        parts.append(f'<text x="{sx(x):.1f}" y="{height - 8}" class="tick" text-anchor="{anchor}">'
                     f'{label}</text>')
    for i, (_lbl, p) in enumerate(pts):
        color = "var(--neg)" if fill_negative else PALETTE[i % len(PALETTE)]
        d = " ".join(f"{'M' if j == 0 else 'L'}{sx(x):.1f},{sy(y):.1f}" for j, (x, y) in enumerate(p))
        if fill_negative:
            area = d + f" L{sx(p[-1][0]):.1f},{sy(0):.1f} L{sx(p[0][0]):.1f},{sy(0):.1f} Z"
            parts.append(f'<path d="{area}" fill="{color}" fill-opacity="0.18" stroke="none"/>')
        parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.6"/>')
    parts.append("</svg>")
    if len(pts) > 1:
        legend = "".join(f'<span><i style="background:{PALETTE[i % len(PALETTE)]}"></i>{escape(lbl)}</span>'
                         for i, (lbl, _) in enumerate(pts))
        parts.append(f'<div class="legend">{legend}</div>')
    return "".join(parts)


def bar_chart(labels: list[str], values: list[float], height: int = 200, title: str = "") -> str:
    """Vertical bars, green for positive and red for negative values."""
    if not values:
        return '<p class="muted">no data</p>'
    width, ml, mr, mt, mb = 900, 64, 16, 16, 40
    lo, hi = min(0.0, min(values)), max(0.0, max(values))
    if lo == hi:
        hi = 1.0
    sy = lambda y: mt + (hi - y) / (hi - lo) * (height - mt - mb)
    n = len(values)
    slot = (width - ml - mr) / n
    bw = max(2.0, slot * 0.7)
    parts = [(f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
             f'aria-label="{escape(title or "bar chart")}">')]
    for t in _nice_ticks(lo, hi, 4):
        parts.append(f'<line x1="{ml}" x2="{width - mr}" y1="{sy(t):.1f}" y2="{sy(t):.1f}" class="grid"/>')
        parts.append(f'<text x="{ml - 6}" y="{sy(t) + 4:.1f}" class="tick" text-anchor="end">{_fmt(t)}</text>')
    every = max(1, n // 24)
    for i, (lbl, v) in enumerate(zip(labels, values, strict=True)):
        x = ml + i * slot + (slot - bw) / 2
        y, h = (sy(v), sy(0) - sy(v)) if v >= 0 else (sy(0), sy(v) - sy(0))
        cls = "pos" if v >= 0 else "neg"
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{max(h, 0.5):.1f}" '
                     f'class="{cls}"><title>{escape(lbl)}: {v:,.2f}</title></rect>')
        if i % every == 0:
            parts.append(f'<text x="{x + bw / 2:.1f}" y="{height - mb + 14}" class="tick" '
                         f'text-anchor="middle">{escape(lbl)}</text>')
    parts.append(f'<line x1="{ml}" x2="{width - mr}" y1="{sy(0):.1f}" y2="{sy(0):.1f}" class="axis"/>')
    parts.append("</svg>")
    return "".join(parts)


def scatter(xs: list[float], ys: list[float], x_label: str, height: int = 180,
            highlight: float | None = None) -> str:
    """Parameter value vs objective (one dot per trial)."""
    pts = [(x, y) for x, y in zip(xs, ys, strict=True) if x is not None and y is not None]
    if not pts:
        return '<p class="muted">no data</p>'
    width, ml, mr, mt, mb = 420, 48, 10, 10, 34
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    if x0 == x1:
        x0, x1 = x0 - 1, x1 + 1
    if y0 == y1:
        y0, y1 = y0 - 1, y1 + 1
    sx = lambda x: ml + (x - x0) / (x1 - x0) * (width - ml - mr)
    sy = lambda y: mt + (y1 - y) / (y1 - y0) * (height - mt - mb)
    parts = [(f'<svg viewBox="0 0 {width} {height}" class="chart small" role="img" '
             f'aria-label="{escape(x_label)} vs objective">')]
    for t in _nice_ticks(y0, y1, 3):
        parts.append(f'<line x1="{ml}" x2="{width - mr}" y1="{sy(t):.1f}" y2="{sy(t):.1f}" class="grid"/>')
        parts.append(f'<text x="{ml - 4}" y="{sy(t) + 4:.1f}" class="tick" text-anchor="end">{_fmt(t)}</text>')
    for x, y in pts:
        parts.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="2.6" class="dot"/>')
    if highlight is not None:
        parts.append(f'<line x1="{sx(highlight):.1f}" x2="{sx(highlight):.1f}" y1="{mt}" '
                     f'y2="{height - mb}" class="hl"/>')
    parts.append(f'<text x="{ml}" y="{height - 18}" class="tick">{_fmt(x0)}</text>')
    parts.append(f'<text x="{width - mr}" y="{height - 18}" class="tick" text-anchor="end">{_fmt(x1)}</text>')
    parts.append(f'<text x="{(ml + width - mr) / 2:.0f}" y="{height - 4}" class="tick" '
                 f'text-anchor="middle">{escape(x_label)}</text>')
    parts.append("</svg>")
    return "".join(parts)
