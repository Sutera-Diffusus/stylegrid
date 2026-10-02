"""workflow.py — 一站式工作流：一张照片 → 全套成品 + 质检报告

快速开始
  python workflow.py --src photo.jpg --out out/auto            # 全自动（推荐先跑这个）
  python workflow.py --src photo.jpg --out out/mine --profile 印刷
  python workflow.py --src photo.jpg --out out/custom --styles pixel_E,glitch_E,cross_E --mixes riso_pixel,ink_point --posters V,S,H
  python workflow.py --list                                    # 看所有风格 / 配方 / 预设

产物（每个输出目录）
  01_九宫格.png    八格 + 原图（风格可指定或自动）
  02_混血格.png    混血配方 3×3（--mixes 指定，或 --no-mix 跳过）
  03_海报_V/S/H    版式海报（--posters 指定，画作用指定风格或混血）
  报告.md          画面分析 + 风格痕迹分 + 文件清单
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
import numpy as np
import cv2
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import gridkit as G
import mixposter as MP

LW = np.array([.2126, .7152, .0722], np.float32)


# ---------------------------------------------------------------- 检验
def trace_score(orig: np.ndarray, out, n: int = 32) -> float:
    """原作痕迹分：0.65×|亮度布局相关| + 0.35×色彩接近度（1.0 = 与原作一致）"""
    a = cv2.resize(orig.astype(np.float32), (n, n), interpolation=cv2.INTER_AREA)
    b = cv2.resize(np.asarray(out, np.float32), (n, n), interpolation=cv2.INTER_AREA)
    ca = abs(float(np.corrcoef((a @ LW).ravel(), (b @ LW).ravel())[0, 1]))
    cd = float(np.mean(np.linalg.norm(a - b, axis=2)))
    return round(0.65 * ca + 0.35 * max(0.0, 1.0 - cd / 180.0), 3)


# ---------------------------------------------------------------- 预设
PROFILES = {
    "照片保真": dict(styles="pixel_E,glitch_E,collage_A,strips_E,weave_E,pointillism_E,newsprint_E,mosaic_E",
                 mixes="riso_pixel,glitch_weave,ink_point", posters="V"),
    "印刷": dict(styles="risomis_A,newsprint_E,cross_E,halftone_E,engraving_E,scanlines_E,stamp_E,foil_E",
              mixes="print_cross,leak_riso,stamp_dada", posters="S"),
    "抽象": dict(styles="opart_E,splatter_E,cubism_E,destijl_C,polar_A,colorfield_B,blocks_E,halftone_E",
              mixes="mosaic_opart,dif_cross_opart", posters="H"),
    "剪影": dict(styles="flat_E,hardedge_E,papercut_E,inkmin_E,flat_A,hardedge_B,papercut_A,inkmin_B",
              mixes="split_flat_pixel,split_risomis_flat", posters="V"),
    "织品": dict(styles="cross_E,quilt_B,weave_C,film_B,mosaic_B,dotmatrix_B,pixel_B,strips_B",
              mixes="lgt_mosaic_glitch,split_weave_neon", posters="S"),
    "全都要": dict(styles="auto", mixes="riso_pixel,print_cross,neon_foil,glitch_weave,ink_point,mosaic_opart,leak_riso,stamp_dada",
                posters="V,S,H"),
}


# ---------------------------------------------------------------- 主流程
def run(src: Path, out: Path, styles=None, mixes=None, posters=None, aspect="1:1",
        size=900, tile=336, margin=22, title="", sub="", poster_style=None, verify=True,
        mode="auto", poly=None, mask=None) -> dict:
    t0 = time.time()
    out.mkdir(parents=True, exist_ok=True)
    report = {"src": str(src), "out": str(out), "steps": [], "files": [], "scores": {}}

    plate = G.Plate(src, work=size, mode=mode, poly=poly, mask=mask, aspect=aspect)
    info = plate.describe()
    report["plate"] = info

    # 1) 风格集合
    if not styles or styles == ["auto"]:
        styles = G.suggest(plate)
        report["styles_from"] = "auto(suggest)"
    else:
        report["styles_from"] = "manual"
    styles = [s for s in styles if s.rsplit("_", 1)[0] in G.STYLES][:8]
    report["styles"] = styles

    # 2) 九宫格
    try:
        G.run(src, out, order=styles, size=size, tile_w=tile, margin=margin, aspect=aspect,
              mode=mode, poly=poly, mask=mask)
        p = out / "nine_grid.png"
        if p.exists():
            p.replace(out / "01_九宫格.png")
            report["files"].append("01_九宫格.png")
            report["steps"].append(("九宫格", "ok"))
        else:
            report["steps"].append(("九宫格", "未产出"))
    except Exception as e:
        report["steps"].append(("九宫格", f"ERR {type(e).__name__}: {e}"))

    # 3) 混血格
    if mixes:
        try:
            names = [m for m in mixes if m in MP.MIXES][:8]
            pieces = {}
            for n in names:
                pieces[n] = Image.fromarray(MP.render_mix(plate, MP.MIXES[n]).astype(np.uint8))
            slots = [0, 1, 2, 3, 5, 6, 7, 8]
            cell = tile + 12
            canvas = Image.new("RGB", (3 * cell + 24, 3 * cell + 24), (236, 233, 227))
            for idx, n in enumerate(names):
                r, c = divmod(slots[idx], 3)
                canvas.paste(pieces[n].resize((tile, tile), Image.LANCZOS), (12 + c * cell, 12 + r * cell))
            canvas.paste(Image.fromarray(plate.orig).resize((tile, tile), Image.LANCZOS),
                         (12 + cell, 12 + cell))
            canvas.save(out / "02_混血格.png")
            report["files"].append("02_混血格.png")
            report["mixes"] = names
            report["steps"].append(("混血格", "ok"))
        except Exception as e:
            report["steps"].append(("混血格", f"ERR {type(e).__name__}: {e}"))

    # 4) 海报
    if posters:
        st = poster_style or (mixes[0] if mixes and mixes[0] in MP.MIXES else styles[0])
        fmt_asp = {"V": "3:4", "S": "1:1", "H": "4:3"}
        for f in posters:
            try:
                MP.poster(src, out / f"03_海报_{f}.png", f, st, title or "作品",
                          sub or "", work=size, aspect=fmt_asp[f])
                report["files"].append(f"03_海报_{f}.png")
            except Exception as e:
                report["steps"].append((f"海报{f}", f"ERR {type(e).__name__}: {e}"))
        report["poster_style"] = st
        report["steps"].append(("海报", "ok"))

    # 5) 质检：逐件痕迹分
    if verify:
        pieces = {}
        for s in styles:
            try:
                pieces[f"{s}"] = G.render(plate, s.rsplit("_", 1)[0], s.rsplit("_", 1)[1], 1)
            except Exception:
                pass
        for n in (report.get("mixes") or []):
            try:
                pieces[f"mix:{n}"] = MP.render_mix(plate, MP.MIXES[n])
            except Exception:
                pass
        report["scores"] = {k: trace_score(plate.orig, v) for k, v in pieces.items()}
        weak = [k for k, v in report["scores"].items() if v < 0.5]
        report["weak"] = weak

    report["seconds"] = round(time.time() - t0, 1)

    # 6) 报告
    L = []
    L.append(f"# 制作报告\n")
    L.append(f"- 源图：`{src.name}`")
    L.append(f"- 尺寸/模式：{info['size']} · {info['mode']} · 主体覆盖 {info['coverage']['bld']}")
    L.append(f"- 画幅：{aspect} · 风格来源：{report['styles_from']}")
    L.append(f"- 耗时：{report['seconds']}s\n")
    L.append("## 使用的风格\n")
    for s in styles:
        L.append(f"- `{s}` {G.NAMES.get(s.rsplit('_', 1)[0], '')}")
    if report.get("mixes"):
        L.append("\n## 混血配方\n")
        for m in report["mixes"]:
            L.append(f"- `{m}`")
    L.append("\n## 质检（原作痕迹分，≥0.5 视为达标）\n")
    if report.get("scores"):
        for k, v in sorted(report["scores"].items(), key=lambda kv: -kv[1]):
            L.append(f"- {'✔' if v >= 0.5 else '⚠'} `{k}` **{v:.2f}**")
    L.append("\n## 产物\n")
    for f in report["files"]:
        L.append(f"- `{f}`")
    if report.get("weak"):
        L.append(f"\n> 低于阈值的：{', '.join('`'+w+'`' for w in report['weak'])}（可换风格或调权重）")
    (out / "报告.md").write_text("\n".join(L), encoding="utf-8")
    return report


def main():
    ap = argparse.ArgumentParser(description="一站式九宫格/混血/海报工作流")
    ap.add_argument("--src")
    ap.add_argument("--out", default="out")
    ap.add_argument("--styles", help="风格列表，或 auto")
    ap.add_argument("--mixes", help="混血配方列表（逗号分隔），或 none")
    ap.add_argument("--posters", help="海报版式：V,S,H 任意组合")
    ap.add_argument("--profile", help="预设名（见 --list）")
    ap.add_argument("--poster-style", help="海报画作用哪个风格/混血")
    ap.add_argument("--title", default="")
    ap.add_argument("--sub", default="")
    ap.add_argument("--aspect", default="1:1", choices=["3:4", "4:3", "1:1"])
    ap.add_argument("--size", type=int, default=900)
    ap.add_argument("--tile", type=int, default=336)
    ap.add_argument("--margin", type=int, default=22)
    ap.add_argument("--mode", default="auto")
    ap.add_argument("--poly")
    ap.add_argument("--mask")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    if a.list:
        print("【风格】%d 种：" % len(G.STYLES))
        for k in sorted(G.STYLES):
            print(f"  {k:12s} {G.NAMES.get(k,'')}")
        print("\n【混血配方】%d 个：" % len(MP.MIXES))
        for k in sorted(MP.MIXES):
            print("  " + k)
        print("\n【预设】：" + "、".join(PROFILES))
        return

    if not a.src:
        ap.error("需要 --src（或用 --list）")
    prof = PROFILES.get(a.profile or "", {})
    styles = (a.styles or prof.get("styles") or "auto").split(",")
    mixes_s = a.mixes if a.mixes is not None else prof.get("mixes", "")
    mixes = [] if mixes_s in ("none", None) else [m for m in str(mixes_s).split(",") if m]
    posters_s = a.posters if a.posters is not None else prof.get("posters", "")
    posters = [p for p in str(posters_s).split(",") if p in ("V", "S", "H")]

    rep = run(Path(a.src), Path(a.out), styles=styles, mixes=mixes, posters=posters,
              aspect=a.aspect, size=a.size, tile=a.tile, margin=a.margin,
              title=a.title, sub=a.sub, poster_style=a.poster_style,
              verify=not a.no_verify, mode=a.mode, poly=a.poly, mask=a.mask)
    print(f"\n完成：{a.out}  用时 {rep['seconds']}s")
    print("产物：" + "、".join(rep["files"]))
    if rep.get("scores"):
        sc = rep["scores"]
        print("痕迹分：" + "  ".join(f"{k}={v:.2f}" for k, v in sorted(sc.items(), key=lambda kv: -kv[1])))
    bad = [f"{k}: {v}" for k, v in rep.get("steps", []) if v != "ok"]
    if bad:
        print("步骤异常：" + "；".join(bad))
    if rep.get("weak"):
        print("⚠ 低于 0.50：" + ", ".join(rep["weak"]))


if __name__ == "__main__":
    main()
