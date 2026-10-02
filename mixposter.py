"""mixposter.py — 风格混血 + 版式系统（独立模块，不动 gridkit 主库）

用法：
  # 混血九宫格（配方名见 MIXES）
  python mixposter.py --src photo.jpg --out out/ --grid riso_pixel,print_cross,...

  # 单张混血
  python mixposter.py --src photo.jpg --out out/ --one "pixel_E*0.75+risomis_A:MUL"

  # 海报版式（竖 V / 方 S / 横 H），可指定画作风格
  python mixposter.py --src photo.jpg --out out/ --poster V --style risomis_A --title "盐湖·骑行"
"""
from __future__ import annotations
import argparse, math, sys
from pathlib import Path
import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent))
import gridkit as G

CJK = G.CJK
CJK_B = G.CJK
LAT = G.LAT


# ------------------------------------------------------------------ 混合模式
def _blend(a: np.ndarray, b: np.ndarray, mode: str) -> np.ndarray:
    if mode == "NRM":
        return b
    if mode == "MUL":
        return a * b / 255.0
    if mode == "SCR":
        return 255.0 - (255.0 - a) * (255.0 - b) / 255.0
    if mode == "OVR":                                     # overlay
        lo = 2 * a * b / 255.0
        hi = 255.0 - 2 * (255.0 - a) * (255.0 - b) / 255.0
        return np.where(a < 127.5, lo, hi)
    if mode == "LGT":
        return np.maximum(a, b)
    if mode == "DRK":
        return np.minimum(a, b)
    if mode == "ADD":
        return np.clip(a + b, 0, 255)
    if mode == "DIF":
        return np.abs(a - b)
    return b


# ------------------------------------------------------------------ 混血配方
# (风格, 配色, k, 模式, 权重, 作用域 A=整体 / S=仅主体 / B=仅背景)
MIXES = {
    "riso_pixel":      [("pixel", "E", 1, "NRM", 1.0, "A"), ("risomis", "A", 1, "MUL", 0.75, "A")],
    "print_cross":     [("cross", "E", 1, "NRM", 1.0, "A"), ("newsprint", "E", 1, "MUL", 0.50, "A")],
    "neon_foil":       [("foil", "E", 1, "NRM", 1.0, "A"), ("neon", "E", 1, "SCR", 0.80, "A")],
    "glitch_weave":    [("weave", "E", 1, "NRM", 1.0, "A"), ("glitch", "B", 1, "LGT", 0.60, "A")],
    "split_flat_pixel": [("pixel", "E", 1, "NRM", 1.0, "B"), ("flat", "A", 1, "NRM", 1.0, "S")],
    "split_riso_fauve": [("fauve", "E", 1, "NRM", 1.0, "B"), ("risomis", "A", 1, "NRM", 1.0, "S")],
    "mosaic_opart":    [("mosaic", "E", 1, "NRM", 1.0, "A"), ("opart", "C", 1, "SCR", 0.50, "A")],
    "cubism_scan":     [("cubism", "E", 1, "NRM", 1.0, "A"), ("scanlines", "B", 1, "OVR", 0.55, "A")],
    "leak_riso":       [("risomis", "A", 1, "NRM", 1.0, "A"), ("lightleak", "E", 1, "SCR", 0.70, "A")],
    "ink_point":       [("pointillism", "E", 1, "NRM", 1.0, "A"), ("inkmin", "B", 1, "MUL", 0.45, "A")],
    "destijl_pixel":   [("destijl", "C", 1, "NRM", 1.0, "A"), ("pixel", "E", 1, "LGT", 0.45, "A")],
    "stamp_dada":      [("dada", "E", 1, "NRM", 1.0, "A"), ("stamp", "E", 1, "OVR", 0.50, "A")],
    "split_weave_neon": [("weave", "C", 1, "NRM", 1.0, "B"), ("neon", "E", 1, "NRM", 1.0, "S")],
    "split_risomis_flat": [("risomis", "A", 1, "NRM", 1.0, "B"), ("flat", "E", 1, "NRM", 1.0, "S")],
    "lgt_mosaic_glitch": [("mosaic", "D", 1, "NRM", 1.0, "A"), ("glitch", "A", 1, "LGT", 0.55, "A")],
    "dif_cross_opart": [("cross", "E", 1, "NRM", 1.0, "A"), ("opart", "B", 1, "DIF", 0.35, "A")],
}


def render_mix(plate: G.Plate, spec) -> np.ndarray:
    """按 [(style, pal, k, mode, weight, where)] 叠加渲染"""
    out = None
    subj = np.clip(plate.bld, 0, 1)[..., None] if plate.bld is not None else 1.0
    for st, pal, kk, mode, w, where in spec:
        lay = np.asarray(G.render(plate, st, pal, kk), np.float32)
        if out is None:
            out = lay
            continue
        mixed = _blend(out, lay, mode)
        if where == "S":
            a = subj * w
        elif where == "B":
            a = (1.0 - subj) * w
        else:
            a = np.float32(w)
        out = out * (1.0 - a) + mixed * a
    return np.clip(out, 0, 255)


def parse_spec(text: str):
    """'pixel_E*1+risomis_A:MUL@B' → [(style,pal,k,mode,w,where), ...]"""
    spec = []
    for part in text.split("+"):
        where = "A"
        if "@" in part:
            part, where = part.split("@", 1)
        mode = "NRM"
        if ":" in part:
            part, mode = part.split(":", 1)
        w = 1.0
        if "*" in part:
            part, ws = part.split("*", 1)
            w = float(ws)
        st, pal = part.rsplit("_", 1)
        spec.append((st, pal, 1, mode.upper(), w, where.upper()))
    return spec


# ------------------------------------------------------------------ 九宫格组装（复用 gridkit 的排版）
def grid_from(plate: G.Plate, outs, out_path: Path, tile=336, margin=22, gut=6):
    return G.make_grid(plate, outs, out_path, tile=tile, margin=margin, gut=gut) \
        if hasattr(G, "make_grid") else None


# ------------------------------------------------------------------ 海报版式
def poster(src: Path, out: Path, fmt: str, style: str, title: str, sub: str,
           work: int = 1152, aspect: str = None) -> Path:
    """竖 V / 方 S / 横 H 三种版式（含题签柱）"""
    asp = aspect or {"V": "3:4", "S": "1:1", "H": "4:3"}[fmt]
    plate = G.Plate(src, work=work, aspect=asp)
    if style in MIXES:                                    # 支持混血配方作为画作
        st = style
        art = render_mix(plate, MIXES[style]).astype(np.uint8)
    else:
        st, pal = style.rsplit("_", 1)
        art = np.asarray(G.render(plate, st, pal, 1)).astype(np.uint8)
    A = Image.fromarray(art)
    paper = (238, 234, 226)
    ink = (26, 26, 30)
    if fmt == "V":
        W, H = int(work * 0.92), int(work * 1.45)
        canvas = Image.new("RGB", (W, H), paper)
        aw = int(W * 0.78)
        a = A.resize((aw, int(A.height * aw / A.width)), Image.LANCZOS)
        canvas.paste(a, (int(W * 0.06), int(W * 0.06)))
        d = ImageDraw.Draw(canvas)
        d.rectangle([int(W * 0.06) - 3, int(W * 0.06) - 3, int(W * 0.06) + aw + 3, int(W * 0.06) + a.height + 3], outline=ink, width=2)
        if title:                                   # 纯图片模式：无标题则不加任何文字
            f = G.font(CJK, int(W * 0.055))
            col_x = int(W * 0.06 + aw + W * 0.045)
            y = int(W * 0.08)
            for ch in title:
                d.text((col_x, y), ch, font=f, fill=ink)
                y += int(W * 0.062)
            d.line([col_x - 8, int(W * 0.06), col_x - 8, int(W * 0.06) + a.height], fill=ink, width=1)
            f2 = G.font(CJK, int(W * 0.020))
            d.text((int(W * 0.06), int(W * 0.06) + a.height + int(W * 0.03)), sub, font=f2, fill=(90, 88, 92))
            d.text((int(W * 0.06) + aw, int(W * 0.06) + a.height + int(W * 0.03)), G.NAMES.get(st, st), font=f2, fill=(90, 88, 92), anchor="ra")
            d.line([int(W * 0.06), int(W * 0.06) + a.height + int(W * 0.022), int(W * 0.06) + aw, int(W * 0.06) + a.height + int(W * 0.022)], fill=(160, 156, 150), width=1)
    elif fmt == "S":
        W = H = int(work * 1.05)
        canvas = Image.new("RGB", (W, H), paper)
        aw = int(W * 0.80)
        a = A.resize((aw, aw), Image.LANCZOS)
        x0 = (W - aw) // 2; y0 = int(H * 0.07)
        canvas.paste(a, (x0, y0))
        d = ImageDraw.Draw(canvas)
        d.rectangle([x0 - 4, y0 - 4, x0 + aw + 4, y0 + aw + 4], outline=ink, width=2)
        if title:
            f = G.font(CJK, int(W * 0.045))
            d.text((x0, y0 + aw + int(H * 0.035)), title, font=f, fill=ink)
            f2 = G.font(CJK, int(W * 0.020))
            d.text((x0, y0 + aw + int(H * 0.035) + int(W * 0.055)), sub, font=f2, fill=(90, 88, 92))
            d.text((x0 + aw, y0 + aw + int(H * 0.035) + int(W * 0.055)), G.NAMES.get(st, st),
                   font=f2, fill=(90, 88, 92), anchor="ra")
    else:  # H
        W, H = int(work * 1.42), int(work * 0.96)
        canvas = Image.new("RGB", (W, H), paper)
        ah = int(H * 0.82)
        a = A.resize((int(A.width * ah / A.height), ah), Image.LANCZOS)
        x0, y0 = int(W * 0.055), (H - ah) // 2
        canvas.paste(a, (x0, y0))
        d = ImageDraw.Draw(canvas)
        d.rectangle([x0 - 3, y0 - 3, x0 + a.width + 3, y0 + ah + 3], outline=ink, width=2)
        if title:
            f = G.font(CJK, int(W * 0.042))
            col_x = x0 + a.width + int(W * 0.05)
            yy = y0 + int(H * 0.02)
            for ch in title:
                d.text((col_x, yy), ch, font=f, fill=ink)
                yy += int(W * 0.048)
            f2 = G.font(CJK, int(W * 0.017))
            d.text((col_x, y0 + ah - int(H * 0.06)), G.NAMES.get(st, st), font=f2, fill=(90, 88, 92))
            d.text((col_x, y0 + ah - int(H * 0.03)), sub, font=f2, fill=(90, 88, 92))
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mix", help="逗号分隔的配方名（九宫格）")
    ap.add_argument("--one", help="自定义混血：'pixel_E*1+risomis_A:MUL'")
    ap.add_argument("--poster", choices=["V", "S", "H"])
    ap.add_argument("--style", default="risomis_A")
    ap.add_argument("--title", default="盐湖 · 骑行")
    ap.add_argument("--sub", default="2026 · 高原盐湖 · 自持骑行")
    ap.add_argument("--size", type=int, default=1000)
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    if a.one:
        plate = G.Plate(Path(a.src), work=a.size, aspect="1:1")
        img = render_mix(plate, parse_spec(a.one))
        Image.fromarray(img.astype(np.uint8)).save(out / "mix_one.png")
        print("one ->", out / "mix_one.png")
    elif a.mix:
        names = [n.strip() for n in a.mix.split(",") if n.strip()]
        plate = G.Plate(Path(a.src), work=a.size, aspect="1:1")
        pieces = {}
        for n in names:
            if n not in MIXES:
                print("  ! 未知配方:", n); continue
            pieces[n] = Image.fromarray(render_mix(plate, MIXES[n]).astype(np.uint8))
        # 组装 3x3（中心放原图）
        tile = int(a.size * 0.42)
        cols = 3
        cell = tile + 12
        canvas = Image.new("RGB", (cols * cell + 24, 3 * cell + 24), (236, 233, 227))
        slots = [0, 1, 2, 3, 5, 6, 7, 8]   # 跳过中心(索引4)
        for idx, n in enumerate(names[:8]):
            im = pieces.get(n)
            if im is None: continue
            r, c = divmod(slots[idx], 3)
            canvas.paste(im.resize((tile, tile), Image.LANCZOS), (12 + c * cell, 12 + r * cell))
        canvas.paste(Image.fromarray(plate.orig).resize((tile, tile), Image.LANCZOS),
                     (12 + cell, 12 + cell))
        p = out / "mix_grid.png"
        canvas.save(p)
        print("grid ->", p, canvas.size)
    elif a.poster:
        p = poster(Path(a.src), out / f"poster_{a.poster}.png", a.poster, a.style, a.title, a.sub)
        print("poster ->", p)


if __name__ == "__main__":
    main()
