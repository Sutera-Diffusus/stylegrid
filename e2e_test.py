"""e2e_test.py — 工作流闭环测试

验证链路：照片 → 工作流（九宫格 / 混血格 / 海报）→ 产物完整性 → 痕迹分达标 → 非退化
用法：python e2e_test.py [照片1 照片2 ...]
"""
from __future__ import annotations
import json, subprocess, sys, tempfile, time
from pathlib import Path
import numpy as np
import cv2
from PIL import Image

HERE = Path(__file__).parent
PY = sys.executable


def img_stats(p: Path):
    a = np.asarray(Image.open(p).convert("RGB")).astype(np.float32)
    return float(a.std()), float((a.max(-1) >= 254).mean() * 100.0)


def run_case(src: Path, out: Path, kwargs, name: str) -> dict:
    t0 = time.time()
    cmd = [PY, str(HERE / "workflow.py"), "--src", str(src), "--out", str(out)] + kwargs
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2400)
    dt = time.time() - t0
    row = {"case": name, "rc": r.returncode, "secs": round(dt, 1), "out": str(out), "fails": []}
    if r.returncode != 0:
        row["fails"].append("crash:" + (r.stderr or "")[-160:].replace("\n", " "))
        return row
    # 1) 产物完整性
    grid = out / "01_九宫格.png"
    rep = out / "报告.md"
    if not grid.exists():
        row["fails"].append("no-grid")
    if not rep.exists():
        row["fails"].append("no-report")
    # 2) 非退化：网格方差/过曝
    if grid.exists():
        sd, clip = img_stats(grid)
        row["std"], row["clip"] = round(sd, 1), round(clip, 2)
        if sd < 8:
            row["fails"].append(f"degenerate(std={sd:.1f})")
        if clip > 3:
            row["fails"].append(f"clip={clip:.1f}%")
    # 3) 海报（若要求）
    for f in ("V", "S", "H"):
        if f"--posters" in kwargs and f in kwargs[kwargs.index("--posters") + 1]:
            if not (out / f"03_海报_{f}.png").exists():
                row["fails"].append(f"no-poster-{f}")
    # 4) 报告里的痕迹分
    if rep.exists():
        txt = rep.read_text(encoding="utf-8")
        scores = []
        for line in txt.splitlines():
            if line.startswith("- ") and "**" in line and "`" in line:
                try:
                    scores.append(float(line.rsplit("**", 2)[1]))
                except Exception:
                    pass
        if scores:
            row["n_scores"] = len(scores)
            row["min_score"] = round(min(scores), 2)
            row["pass_rate"] = round(sum(1 for s in scores if s >= 0.5) / len(scores), 2)
            if row["pass_rate"] < 0.6:
                row["fails"].append(f"score-pass={row['pass_rate']:.0%}")
    row["verdict"] = "PASS" if not row["fails"] else "FAIL:" + ",".join(row["fails"])
    return row


def main():
    args = [Path(a) for a in sys.argv[1:]]
    if not args:                                    # 无参数：先用合成用例自检
        import selftest as ST
        d = HERE / "_selftest" / "inputs"
        d.mkdir(parents=True, exist_ok=True)
        ST.synth_cases(d)
        args = [d / "syn_night_tower.png", d / "syn_day_building.png"]
    args = [a for a in args if a.exists()]
    cases = []
    tmp = Path(tempfile.mkdtemp(prefix="sg_e2e_"))
    for i, src in enumerate(args):
        stem = src.stem
        cases.append((f"{stem}/自动", src, tmp / f"{stem}_auto",
                      ["--mixes", "riso_pixel,ink_point", "--posters", "V"]))
        cases.append((f"{stem}/纯图海报全版式", src, tmp / f"{stem}_poster",
                      ["--styles", "pixel_E,glitch_E,cross_E,mosaic_E,strips_E,collage_A,pointillism_E,newsprint_E",
                       "--mixes", "none", "--posters", "V,S,H"]))
    rows = []
    for name, src, out, kw in cases:
        try:
            row = run_case(src, out, kw, name)
        except Exception as e:
            row = {"case": name, "verdict": f"FAIL:exc {type(e).__name__}", "fails": [str(e)[:80]]}
        rows.append(row)
        print(f"  {row['verdict']:28s} {name:34s} {row.get('secs','-')}s  "
              f"痕迹分 min={row.get('min_score','-')} 通过率={row.get('pass_rate','-')}")
    npass = sum(1 for r in rows if r["verdict"] == "PASS")
    print(f"\n== 闭环测试 {npass}/{len(rows)} PASS")
    outj = HERE / "_selftest" / "e2e_report.json"
    outj.parent.mkdir(exist_ok=True)
    outj.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("明细:", outj)
    return 0 if npass == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
