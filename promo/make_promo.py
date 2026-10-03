"""Generate a self-contained Stylegrid promotional short film.

Light, minimal iOS-style "white gallery" theme matching the product web UI.
Renders a 15 second 960x540 video to promo/out/ plus a contact sheet, a
poster frame, and a JSON manifest.  PIL + numpy + OpenCV only, no network.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# The script location, rather than cwd, is the source of truth.  This makes
# both `python promo/make_promo.py` and `cd promo && python make_promo.py`
# produce the same artifact directory.
PROMO_DIR = Path(__file__).resolve().parent
ROOT = PROMO_DIR.parent
SAMPLES = ROOT / "samples"
OUT = PROMO_DIR / "out"

WIDTH, HEIGHT = 960, 540
FPS = 24
DURATION = 15.0
TOTAL_FRAMES = int(round(FPS * DURATION))
SEED = 2309

# Design tokens shared with the new web UI.
INK = (29, 29, 31)          # #1D1D1F primary text
SECONDARY = (134, 134, 139)  # #86868B secondary text
ACCENT = (0, 122, 255)      # #007AFF kickers / small labels only
BG_TOP = (255, 255, 255)    # #FFFFFF
BG_BOTTOM = (245, 245, 247)  # #F5F5F7

# Tiny timeline cards: start/end are in seconds and are also recorded in JSON.
SCENES = [
    {"id": "open", "start": 0.0, "end": 3.4, "label": "开场 / 一张照片进"},
    {"id": "grid", "start": 3.4, "end": 7.2, "label": "九宫格 / 51 种风格"},
    {"id": "posters", "start": 7.2, "end": 11.0, "label": "海报 / V·S·H"},
    {"id": "close", "start": 11.0, "end": 15.0, "label": "收尾 / 本地运行"},
]


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def ease(value: float) -> float:
    """Smooth cubic easing used for all card entrances and exits."""
    x = clamp(value)
    return x * x * (3.0 - 2.0 * x)


def phase(t: float, start: float, end: float, edge: float = 0.28) -> float:
    """Return a 0..1 progress with a short eased entrance/exit."""
    raw = clamp((t - start) / max(0.001, end - start))
    ins = ease(clamp(raw / max(edge / (end - start), 0.001)))
    outs = ease(clamp((1.0 - raw) / max(edge / (end - start), 0.001)))
    return ins * outs


def font_candidates() -> list[Path]:
    """Find common installed fonts without downloading anything."""
    home = Path.home()
    windir = Path(os.environ.get("WINDIR", "C:/Windows"))
    candidates = [
        # Chinese-capable fonts first on Windows.
        windir / "Fonts" / "msyh.ttc",
        windir / "Fonts" / "msyhbd.ttc",
        windir / "Fonts" / "simhei.ttf",
        windir / "Fonts" / "simsun.ttc",
        windir / "Fonts" / "Deng.ttf",
        windir / "Fonts" / "SourceHanSansSC-Regular.otf",
        home / ".fonts" / "NotoSansCJK-Regular.ttc",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    seen: set[str] = set()
    return [p for p in candidates if p.exists() and not (str(p) in seen or seen.add(str(p)))]


_FONT_PATHS = font_candidates()


def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    # Use a bold face when discoverable, otherwise Pillow's regular face still
    # renders reliably.  The default bitmap font is the final no-font fallback.
    paths = list(_FONT_PATHS)
    if bold:
        paths = [
            p
            for p in (
                Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyhbd.ttc",
                Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "simhei.ttf",
            )
            if p.exists()
        ] + paths
    for path in paths:
        try:
            return ImageFont.truetype(str(path), size=size)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def load_assets() -> dict[str, Image.Image]:
    names = {
        "hero": "hero.jpg",
        "bike_grid": "preview_bike_grid.jpg",
        "bike_mix": "preview_bike_mix.jpg",
        "moon_grid": "preview_moon_grid.jpg",
        "moon_mix": "preview_moon_mix.jpg",
        "bike_v": "preview_bike_poster_v.jpg",
        "bike_s": "preview_bike_poster_s.jpg",
        "bike_h": "preview_bike_poster_h.jpg",
        "moon_v": "preview_moon_poster_v.jpg",
        "moon_s": "preview_moon_poster_s.jpg",
        "moon_h": "preview_moon_poster_h.jpg",
    }
    assets: dict[str, Image.Image] = {}
    missing: list[str] = []
    for key, filename in names.items():
        path = SAMPLES / filename
        try:
            assets[key] = Image.open(path).convert("RGB")
        except (OSError, FileNotFoundError):
            missing.append(filename)
    if missing:
        raise FileNotFoundError("Missing local sample assets: " + ", ".join(missing))
    # samples/hero.jpg 本身是 3×3 成品拼图;取中心一格(写实照片)作为"原图"使用。
    hero = assets["hero"]
    w, h = hero.size
    assets["hero_single"] = hero.crop((w // 3, h // 3, 2 * w // 3, 2 * h // 3))
    return assets


def crop_to(image: Image.Image, size: tuple[int, int], anchor: tuple[float, float] = (0.5, 0.5)) -> Image.Image:
    """Cover-crop an image to a frame while preserving the subject anchor."""
    tw, th = size
    iw, ih = image.size
    scale = max(tw / iw, th / ih)
    nw, nh = max(tw, int(iw * scale)), max(th, int(ih * scale))
    resized = image.resize((nw, nh), Image.Resampling.LANCZOS)
    left = int(clamp(anchor[0]) * max(0, nw - tw))
    top = int(clamp(anchor[1]) * max(0, nh - th))
    return resized.crop((left, top, left + tw, top + th))


def tint(image: Image.Image, color: tuple[int, int, int], amount: float) -> Image.Image:
    layer = Image.new("RGB", image.size, color)
    return Image.blend(image, layer, clamp(amount))


def paste_cover(canvas: Image.Image, image: Image.Image, box: tuple[int, int, int, int], alpha: float = 1.0,
                anchor: tuple[float, float] = (0.5, 0.5), radius: int = 0) -> None:
    x, y, w, h = box
    tile = crop_to(image, (max(1, w), max(1, h)), anchor)
    if radius:
        mask = Image.new("L", tile.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=radius, fill=int(255 * clamp(alpha)))
        canvas.paste(tile, (x, y), mask)
    elif alpha < 1.0:
        canvas.paste(tile, (x, y), Image.new("L", tile.size, int(255 * clamp(alpha))))
    else:
        canvas.paste(tile, (x, y))


def paste_card(canvas: Image.Image, image: Image.Image, box: tuple[int, int, int, int], alpha: float = 1.0,
               radius: int = 18, anchor: tuple[float, float] = (0.5, 0.5)) -> None:
    """Paste a cover-cropped rounded image with a soft drop shadow beneath."""
    x, y, w, h = box
    blur, offset_y = 14, 10
    pad = blur * 2 + abs(offset_y)
    shadow = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle((pad, pad + offset_y, pad + w, pad + offset_y + h), radius=radius,
                         fill=(0, 0, 0, int(45 * clamp(alpha))))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    canvas.paste(shadow, (x - pad, y - pad), shadow)
    paste_cover(canvas, image, (x, y, w, h), alpha, anchor=anchor, radius=radius)


def text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def draw_text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, font: ImageFont.ImageFont,
              fill: tuple[int, int, int] | tuple[int, int, int, int], anchor: str = "la",
              stroke: int = 0, stroke_fill: tuple[int, int, int] = (0, 0, 0)) -> None:
    draw.text((int(xy[0]), int(xy[1])), text, font=font, fill=fill, anchor=anchor,
              stroke_width=stroke, stroke_fill=stroke_fill)


def fade(color: tuple[int, int, int], amount: float, bg: tuple[int, int, int] = (250, 250, 251)) -> tuple[int, int, int]:
    """Lerp a text colour toward the light background to fake an alpha fade."""
    a = clamp(amount)
    return tuple(int(round(bg[i] + (color[i] - bg[i]) * a)) for i in range(3))


def draw_base(t: float) -> Image.Image:
    """Static light vertical gradient: #FFFFFF at top to #F5F5F7 at bottom."""
    yy = np.linspace(0, 1, HEIGHT, dtype=np.float32)[:, None, None]
    top = np.array(BG_TOP, dtype=np.float32)
    bottom = np.array(BG_BOTTOM, dtype=np.float32)
    rgb = top * (1.0 - yy) + bottom * yy
    rgb = np.broadcast_to(rgb, (HEIGHT, WIDTH, 3)).copy()
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB")


def render_frame(t: float, frame: int, assets: dict[str, Image.Image]) -> Image.Image:
    scene = next((s for s in SCENES if s["start"] <= t < s["end"]), SCENES[-1])
    image = draw_base(t)
    draw = ImageDraw.Draw(image)
    sid = scene["id"]
    p = phase(t, scene["start"], scene["end"])

    if sid == "open":
        q = ease(clamp((t - scene["start"]) / 0.6))
        dy = int((1 - q) * 36)
        paste_card(image, assets["hero_single"], (520, 70 + dy, 380, 400), q * p, radius=18, anchor=(0.5, 0.45))
        ty = int((1 - q) * 24)
        a = q * p
        draw_text(draw, (70, 120 + ty), "STYLEGRID · 本地风格实验室", get_font(16, bold=True), fade(ACCENT, a))
        draw_text(draw, (70, 170 + ty), "一张照片进，", get_font(60, bold=True), fade(INK, a))
        draw_text(draw, (70, 250 + ty), "一套作品出。", get_font(60, bold=True), fade(INK, a))
        draw_text(draw, (70, 330 + ty), "51 种艺术风格 · 16 个混血配方 · V / S / H 海报",
                  get_font(18), fade(SECONDARY, a))

    elif sid == "grid":
        draw_text(draw, (70, 64), "九宫格", get_font(16, bold=True), fade(ACCENT, p))
        for i, key in enumerate(("bike_grid", "moon_grid")):
            q = ease(clamp((t - scene["start"] - i * 0.15) / 0.5))
            dy = int((1 - q) * 30)
            paste_card(image, assets[key], (90 + i * 440, 100 + dy, 340, 340), q * p, radius=18)
        cq = ease(clamp((t - scene["start"] - 0.3) / 0.5))
        draw_text(draw, (WIDTH // 2, 480 + int((1 - cq) * 16)), "同一张照片，多种视觉语法",
                  get_font(20), fade(SECONDARY, cq * p), anchor="ma")

    elif sid == "posters":
        draw_text(draw, (70, 64), "海报变体", get_font(16, bold=True), fade(ACCENT, p))
        posters = [("V", "bike_v", 15, 210, 375), ("S", "bike_s", 265, 300, 315), ("H", "bike_h", 605, 340, 240)]
        for i, (tag, key, x, w, h) in enumerate(posters):
            q = ease(clamp((t - scene["start"] - i * 0.12) / 0.5))
            y = 285 - h // 2 + int((1 - q) * 30)
            paste_card(image, assets[key], (x, y, w, h), q * p, radius=18, anchor=(0.5, 0.45))
            draw_text(draw, (x + w // 2, y + h + 26), tag, get_font(15, bold=True), fade(ACCENT, q * p),
                      anchor="ma")

    else:  # close
        delays = (0.0, 0.1, 0.2, 0.3)
        q0 = ease(clamp((t - scene["start"] - delays[0]) / 0.5))
        paste_card(image, assets["hero_single"], (480 - 48, 130 + int((1 - q0) * 24), 96, 96), q0 * p,
                   radius=24, anchor=(0.5, 0.45))
        lines = [
            (1, "stylegrid", 64, True, INK, 270),
            (2, "全部本地运行，素材不离开设备", 20, False, SECONDARY, 340),
            (3, "一张照片进，一套作品出", 16, True, ACCENT, 385),
        ]
        for i, text, size, bold, color, y in lines:
            q = ease(clamp((t - scene["start"] - delays[i]) / 0.5))
            draw_text(draw, (WIDTH // 2, y + int((1 - q) * 24)), text, get_font(size, bold=bold),
                      fade(color, q * p), anchor="ma")

    return image


def make_contact_sheet(frames: list[tuple[float, Image.Image]], path: Path) -> None:
    tile_w, tile_h = 320, 180
    sheet = Image.new("RGB", (tile_w * 3, tile_h * 3 + 54), BG_BOTTOM)
    draw = ImageDraw.Draw(sheet)
    draw_text(draw, (18, 18), "stylegrid · 宣传片分镜", get_font(17, bold=True), INK)
    for i, (t, frame) in enumerate(frames):
        x = (i % 3) * tile_w
        y = 54 + (i // 3) * tile_h
        sheet.paste(frame.resize((tile_w, tile_h), Image.Resampling.LANCZOS), (x, y))
        draw.rectangle((x, y, x + tile_w - 1, y + tile_h - 1), outline=(224, 224, 228), width=1)
        draw.rectangle((x + 7, y + 7, x + 74, y + 28), fill=(255, 255, 255), outline=(224, 224, 228))
        draw_text(draw, (x + 13, y + 17), f"{t:04.1f}s", get_font(12, bold=True), INK, anchor="lm")
    sheet.save(path, quality=94)


def write_video(frames: Iterable[Image.Image], path: Path) -> tuple[bool, str]:
    """Try codecs in order and return (success, codec name)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    for codec in ("mp4v", "avc1", "H264"):
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*codec), FPS, (WIDTH, HEIGHT))
        if not writer.isOpened():
            writer.release()
            continue
        count = 0
        try:
            for frame in frames:
                writer.write(cv2.cvtColor(np.asarray(frame), cv2.COLOR_RGB2BGR))
                count += 1
        finally:
            writer.release()
        if count == TOTAL_FRAMES and path.exists() and path.stat().st_size > 0:
            return True, codec
        try:
            path.unlink()
        except OSError:
            pass
    return False, "none"


def write_gif(frames: list[Image.Image], path: Path) -> bool:
    """Portable fallback animation with one shared palette for stable colour."""
    reduced_rgb = [frame.resize((480, 270), Image.Resampling.BILINEAR).convert("RGB") for frame in frames[::2]]
    if not reduced_rgb:
        return False
    try:
        palette_source = reduced_rgb[0].quantize(colors=256, method=Image.Quantize.MEDIANCUT)
        reduced = [frame.quantize(palette=palette_source, dither=Image.Dither.NONE) for frame in reduced_rgb]
        reduced[0].save(path, save_all=True, append_images=reduced[1:], duration=1000 // 12,
                         loop=0, optimize=False, disposal=2)
        return path.exists() and path.stat().st_size > 0
    except (OSError, ValueError):
        return False


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    assets = load_assets()
    # Render once in memory: this lets us create the MP4, contact sheet, and
    # poster frame from exactly the same frame-accurate source.
    frames: list[Image.Image] = []
    for frame_no in range(TOTAL_FRAMES):
        frames.append(render_frame(frame_no / FPS, frame_no, assets))

    poster_path = OUT / "stylegrid_promo_poster.png"
    frames[int(9.85 * FPS)].save(poster_path, optimize=True)
    contact_times = [0.6, 1.6, 3.0, 4.0, 5.5, 7.8, 9.5, 12.0, 14.2]
    contacts = [(t, frames[min(TOTAL_FRAMES - 1, int(round(t * FPS)))]) for t in contact_times]
    contact_path = OUT / "stylegrid_promo_contact_sheet.jpg"
    make_contact_sheet(contacts, contact_path)

    mp4_path = OUT / "stylegrid_promo.mp4"
    fallback_path = OUT / "stylegrid_promo_fallback.gif"
    frame_dir = OUT / "stylegrid_promo_frames"
    for stale in (fallback_path,):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
    if frame_dir.exists():
        shutil.rmtree(frame_dir)
    ok, codec = write_video(frames, mp4_path)
    artifact: str
    fallback = False
    artifact_width, artifact_height, artifact_fps, artifact_frames = WIDTH, HEIGHT, FPS, TOTAL_FRAMES
    if ok:
        artifact = mp4_path.name
    else:
        fallback = True
        if not write_gif(frames, fallback_path):
            frame_dir.mkdir(exist_ok=True)
            for i, frame in enumerate(frames):
                frame.save(frame_dir / f"frame_{i:04d}.png")
            artifact = frame_dir.name
        else:
            artifact = fallback_path.name
            artifact_width, artifact_height, artifact_fps = 480, 270, 12
            artifact_frames = len(frames[::2])
        try:
            mp4_path.unlink()
        except FileNotFoundError:
            pass

    metadata = {
        "title": "Stylegrid — 本地风格实验室",
        "language": "zh-CN + en",
        "artifact": artifact,
        "fallback": fallback,
        "codec": codec if not fallback else "gif" if artifact.endswith(".gif") else "png-sequence",
        "duration_seconds": round(artifact_frames / artifact_fps, 3),
        "fps": artifact_fps,
        "frame_count": artifact_frames,
        "size": {"width": artifact_width, "height": artifact_height, "aspect": "16:9"},
        "contact_sheet": contact_path.name,
        "poster_frame": poster_path.name,
        "assets": sorted({str((SAMPLES / name).relative_to(ROOT)).replace("\\", "/") for name in {
            "hero.jpg", "preview_bike_grid.jpg", "preview_bike_mix.jpg", "preview_moon_grid.jpg",
            "preview_moon_mix.jpg", "preview_bike_poster_v.jpg", "preview_bike_poster_s.jpg",
            "preview_bike_poster_h.jpg", "preview_moon_poster_v.jpg", "preview_moon_poster_s.jpg",
            "preview_moon_poster_h.jpg",
        }}),
        "scenes": SCENES,
        "claims": {
            "local_run": "本地运行 / offline-first demo copy",
            "styles": 51,
            "recipes": 16,
            "trace_qc": "示例动画；实际痕迹分由 workflow.py 报告计算",
            "deepseek": "creative director concept layer only; no network API configured",
        },
    }
    (OUT / "stylegrid_promo.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Stylegrid promo: {artifact}")
    print(f"Contact sheet: {contact_path}")
    print(f"Poster frame: {poster_path}")
    if fallback:
        print("MP4 codec unavailable; wrote a clearly named fallback animation/sequence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
