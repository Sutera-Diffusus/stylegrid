"""selftest.py — 反复测试：多图 × 自动推荐，自动判定失败/退化
用法:  python selftest.py [图片1 图片2 ...]       (不给则用合成用例 + 工作区样图)
输出:  _selftest/report.csv  _selftest/sheet.png
"""
import csv, subprocess, sys, time, json
import numpy as np
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

KIT = Path(__file__).parent
OUT = KIT / "_selftest"; OUT.mkdir(exist_ok=True)
PY = sys.executable


def synth_cases(d: Path):
    """程序生成边界用例，任何机器都能跑"""
    rng = np.random.default_rng(0)
    H, W = 900, 675
    # 1 夜塔+月（目标场景）
    im = np.zeros((H, W, 3), np.float32); im[..., :] = (10, 18, 46)
    yy, xx = np.mgrid[0:H, 0:W]
    for y in range(H):
        im[y] = np.array([8, 14, 40]) + (y / H) * np.array([6, 10, 20])
    tower = ((np.abs(xx - W * 0.5) < 60) & (yy > H * 0.45) & (yy < H * 0.95))
    base = (yy > H * 0.78)
    im[tower | base] = (215, 212, 205)
    cv2m = ((xx - W * 0.42) ** 2 + (yy - H * 0.12) ** 2) < 42 ** 2
    im[cv2m] = (235, 150, 90)
    Image.fromarray(np.clip(im, 0, 255).astype(np.uint8)).save(d / "syn_night_tower.png")
    # 2 白天建筑（蓝天空 + 暖色建筑）
    im = np.zeros((H, W, 3), np.float32)
    for y in range(H):
        im[y] = np.array([120, 165, 230]) * (1 - 0.25 * y / H)
    b = (yy > H * 0.55) & (xx > W * 0.15) & (xx < W * 0.85)
    im[b] = (205, 185, 150)
    Image.fromarray(np.clip(im, 0, 255).astype(np.uint8)).save(d / "syn_day_building.png")
    # 3 纯灰低对比（退化用例，应被标记）
    g = 118 + rng.normal(0, 3, (H, W))
    Image.fromarray(np.clip(np.stack([g] * 3, -1), 0, 255).astype(np.uint8)).save(d / "syn_flat_gray.png")
    # 4 暗夜城市
    im = np.zeros((H, W, 3), np.float32) + 6
    for _ in range(120):
        x = int(rng.integers(0, W - 40)); y = int(rng.integers(int(H * 0.4), H - 20))
        w = int(rng.integers(6, 40)); h = int(rng.integers(20, 120))
        x1, y1 = min(W, x + w), min(H, y + h)
        im[y:y1, x:x1] = (rng.uniform(20, 90), rng.uniform(20, 90), rng.uniform(30, 110))
        for __ in range(int(rng.integers(3, 20))):
            im[int(rng.integers(y, y1)), int(rng.integers(x, x1))] = (230, 220, 170)
    Image.fromarray(np.clip(im, 0, 255).astype(np.uint8)).save(d / "syn_dark_city.png")
    # 5 方图（测 1:1）
    im = np.zeros((760, 760, 3), np.float32) + 15
    cv2m = ((np.mgrid[0:760, 0:760][0] - 500) ** 2 + (np.mgrid[0:760, 0:760][1] - 300) ** 2) < 70 ** 2
    im[cv2m] = (240, 130, 110)
    Image.fromarray(np.clip(im, 0, 255).astype(np.uint8)).save(d / "syn_square.png")


def workspace_cases():
    d = KIT / "_selftest" / "inputs"
    pool = [d / n for n in ("new_bike.jpg", "syn_night_tower.png", "syn_day_building.png",
                            "syn_dark_city.png", "syn_square.png")]
    return [p for p in pool if p.exists()]


def run_case(src: Path, aspect: str, tag: str):
    outd = OUT / tag; outd.mkdir(exist_ok=True)
    t0 = time.time()
    r = subprocess.run([PY, str(KIT / "gridkit.py"), "--src", str(src), "--out", str(outd),
                        "--aspect", aspect, "--suggest", "--size", "768", "--tile", "300", "--margin", "20"],
                       capture_output=True, text=True, timeout=1200)
    dt = time.time() - t0
    info, sug = {}, ""
    for line in (r.stdout or "").splitlines():
        if line.startswith("plate:"):
            try: info = json.loads(line.split("plate:", 1)[1])
            except Exception: pass
        if line.startswith("suggest:"): sug = line.split("suggest:", 1)[1].strip()
    gp = outd / "nine_grid.png"
    row = dict(image=src.name, tag=tag, aspect=aspect, rc=r.returncode, mode=info.get("mode", "?"),
               bld=info.get("coverage", {}).get("bld"), focal=bool(info.get("focal")),
               secs=round(dt, 1), grid=gp.exists(), suggest=sug)
    if gp.exists():
        a = np.asarray(Image.open(gp).convert("RGB")).astype(np.float32)
        row["std"] = round(float(a.std()), 1)
        row["clip%"] = round(float((a.max(-1) >= 254).mean() * 100), 2)
        row["flat%"] = round(float((np.abs(np.diff(a.mean(-1), axis=0)) < 0.5).mean() * 100), 1)
    # 判定
    fails = []
    if r.returncode != 0: fails.append("crash")
    if not row["grid"]: fails.append("no-grid")
    if row.get("bld") is not None and not (0.02 <= row["bld"] <= 0.98): fails.append(f"bld={row['bld']}")
    if row.get("std") is not None and row["std"] < 8: fails.append(f"degenerate(std={row['std']})")
    if row.get("clip%") is not None and row["clip%"] > 3: fails.append(f"clip={row['clip%']}%")
    row["verdict"] = "PASS" if not fails else "WARN:" + ",".join(fails)
    return row


def main():
    args = sys.argv[1:]
    d = OUT / "inputs"; d.mkdir(exist_ok=True)
    synth_cases(d)
    cases = [d / n for n in ["syn_night_tower.png", "syn_day_building.png", "syn_flat_gray.png",
                             "syn_dark_city.png", "syn_square.png"]]
    cases += [Path(a) for a in args] if args else workspace_cases()
    rows = []
    for c in cases:
        for aspect, tag in [(("1:1" if c.stem == "syn_square" else "3:4"), f"{c.stem}")]:
            row = run_case(c, aspect, tag)
            rows.append(row)
            print(f"{row['image']:30s} {row['aspect']:4s} {row['verdict']:26s} mode={row['mode']:8s} "
                  f"bld={row['bld']} focal={row['focal']} std={row.get('std')} clip={row.get('clip%')} {row['secs']}s")
    with open(OUT / "report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    # 拼图
    tiles = [(r["image"], Image.open(OUT / r["tag"] / "nine_grid.png").convert("RGB")) for r in rows if r["grid"]]
    tw = 210; th = int(tw * (tiles[0][1].height / tiles[0][1].width)) if tiles else 280
    cols = min(5, max(1, len(tiles)))
    rowsn = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (tw + 8) + 8, rowsn * (th + 26) + 8), (236, 234, 229))
    dr = ImageDraw.Draw(sheet); f = ImageFont.truetype("C:/Windows/Fonts/NotoSansSC-VF.ttf", 13)
    for i, (nm, im) in enumerate(tiles):
        r_, c_ = divmod(i, cols)
        x = 8 + c_ * (tw + 8); y = 8 + r_ * (th + 26)
        sheet.paste(im.resize((tw, th), Image.LANCZOS), (x, y))
        verdict = next((rr["verdict"] for rr in rows if rr["image"] == nm), "")
        dr.text((x, y + th + 3), f"{nm[:22]} {verdict[:22]}", font=f, fill=(40, 40, 44))
    sheet.save(OUT / "sheet.png")
    npass = sum(1 for r in rows if r["verdict"] == "PASS")
    print(f"\n== {npass}/{len(rows)} PASS  -> {OUT/'report.csv'}  {OUT/'sheet.png'}")


if __name__ == "__main__":
    main()
