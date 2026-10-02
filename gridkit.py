"""gridkit.py — 九宫格艺术风格工作流 (reusable pipeline)
===============================================================================
用法示例
  1) 看有哪些风格 / 配色:      python gridkit.py --list
  2) 一键出九宫格:             python gridkit.py --src photo.jpg --out out/
  3) 指定八张风格与中心图:      python gridkit.py --src photo.jpg --out out/ \
        --center photo --order hardedge_A,halftone_B,negative_B,engraving_A,pattern_B,collage_A,polar_C,duotone_C
  4) 只出单张:                 python gridkit.py --src photo.jpg --out out/ --single polar_C
输出: out/nine_grid.png(+jpg)  或  out/pieces/*.png
------------------------------------------------------------------------------
管线: 读图 → 清理底盘(清雾/去斑/月亮检测) → 元素蒙版(天空/亮面/暗面/开口/月)
      → 风格渲染(元素配色, 无文字) → 九宫格组装(出血/纸白边)
关键设计: 所有蒙版自动求阈值(不写死), 失败有回退; 风格只依赖元素蒙版, 因此可无限扩展。
"""
from __future__ import annotations
import argparse, json, math, sys
import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

# ---------------------------------------------------------------- 基础工具
LW = np.array([.2126, .7152, .0722], np.float32)
CB = lambda a: np.ascontiguousarray(a, dtype=np.float32)
GB = lambda a, s: cv2.GaussianBlur(CB(a), (0, 0), s)
def _pick_font(cands):
    for c in cands:
        try:
            if Path(c).exists():
                return c
        except Exception:
            pass
    return None


CJK = _pick_font(["C:/Windows/Fonts/NotoSansSC-VF.ttf", "C:/Windows/Fonts/msyh.ttc",
                  "/System/Library/Fonts/PingFang.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                  "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"])
LAT = _pick_font(["C:/Windows/Fonts/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])


def font(path, size, axes=None):
    try:
        f = ImageFont.truetype(path or "arial.ttf", size)
    except Exception:
        f = ImageFont.load_default()
    if axes:
        try:
            f.set_variation_by_axes(axes)
        except Exception:
            pass
    return f


def clean_mask(m, min_area=220, med=9):
    m = cv2.medianBlur((m > 0).astype(np.uint8) * 255, med) // 255
    n, lab, st, _ = cv2.connectedComponentsWithStats(m.astype(np.uint8), 8)
    keep = np.zeros_like(m)
    for i in range(1, n):
        if st[i, cv2.CC_STAT_AREA] >= min_area:
            keep[lab == i] = 1
    return (keep * m).astype(np.uint8)



def _mask_score(m: np.ndarray, W: int, H: int, L: np.ndarray | None = None) -> float:
    """给“主体候选掩版”打分：像一个居中/居下的独立主体 = 高分；糊满画框/贴顶 = 低分"""
    m = (m > 0).astype(np.uint8)
    cov = float(m.mean())
    if cov < 0.015 or cov > 0.95:
        return -9.0
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, 8)
    if n <= 1:
        return -5.0
    areas = st[1:, cv2.CC_STAT_AREA].astype(np.float64)
    main = float(areas.max() / max(1.0, areas.sum()))
    k = max(2, min(H, W) // 40)
    ring = np.zeros_like(m); ring[:k] = 1; ring[-k:] = 1; ring[:, :k] = 1; ring[:, -k:] = 1
    bfrac = float((m * ring).sum() / max(ring.sum(), 1))
    sc = 1.0 if 0.04 <= cov <= 0.75 else (0.3 if cov <= 0.9 else -1.0)
    sc += 0.55 * main
    sc -= 1.5 * max(0.0, bfrac - 0.25)
    if cov < 0.05:
        sc -= 0.8                     # 极小掩版的边界天然容易贴到强边，防止“以小吃大”
    ys, _ = np.where(m > 0)
    if len(ys):
        cy = float(ys.mean()) / H
        sc += 0.4 if 0.25 <= cy <= 0.85 else -0.5
    if L is not None:
        sc += 1.8 * _edge_fit(m, L)            # 边界贴合真实边缘 = 精准抓取
    return sc


def _edge_fit(m: np.ndarray, L: np.ndarray) -> float:
    """边缘测算：掩版边界像素处的图像梯度均值（相对 98 分位归一化）——越高说明边界越贴合真实边缘"""
    b = (m > 0).astype(np.uint8)
    ring = b & ~(cv2.erode(b, np.ones((3, 3), np.uint8)) > 0)
    if ring.sum() < 10:
        return 0.0
    gl = cv2.GaussianBlur(CB(L), (0, 0), 1.2)
    gx = cv2.Sobel(gl, cv2.CV_32F, 1, 0, 3); gy = cv2.Sobel(gl, cv2.CV_32F, 0, 1, 3)
    g = np.sqrt(gx * gx + gy * gy)
    ref = float(np.percentile(g, 98)) + 1e-6
    return float(np.clip(g[ring > 0].mean() / ref, 0, 1))



def _bbox_restrict(m: np.ndarray, keep_frac: float = 0.08, pad: float = 0.05) -> np.ndarray:
    """只保留“显著连通块”的联合包围盒内的像素：远处的背景带（山脊/云带）不进入后续 FG 先验"""
    n, lab, st, _ = cv2.connectedComponentsWithStats((m > 0).astype(np.uint8), 8)
    if n <= 1:
        return m
    main = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
    thr = keep_frac * float(st[main, cv2.CC_STAT_AREA])
    H, W = m.shape
    x0, y0, x1, y1 = W, H, 0, 0
    for i in range(1, n):
        if float(st[i, cv2.CC_STAT_AREA]) >= thr:
            x0 = min(x0, int(st[i, cv2.CC_STAT_LEFT])); y0 = min(y0, int(st[i, cv2.CC_STAT_TOP]))
            x1 = max(x1, int(st[i, cv2.CC_STAT_LEFT] + st[i, cv2.CC_STAT_WIDTH]))
            y1 = max(y1, int(st[i, cv2.CC_STAT_TOP] + st[i, cv2.CC_STAT_HEIGHT]))
    if x1 <= x0 or y1 <= y0:
        return m
    px, py = int((x1 - x0) * pad), int((y1 - y0) * pad)
    out = np.zeros_like(m)
    out[max(0, y0 - py):min(H, y1 + py), max(0, x0 - px):min(W, x1 + px)] = \
        m[max(0, y0 - py):min(H, y1 + py), max(0, x0 - px):min(W, x1 + px)]
    return out

def _refine_mask(P: np.ndarray, raw: np.ndarray, iters: int = 4) -> np.ndarray:
    """物体级分割（高分辨率 + 掩版初始化）：
    - 分辨率 ~1100px：细管/缝隙不被下采样吃掉；
    - FG/BG 先验来自逐像素掩版：轮圈内、车架三角这些“包围盒里的背景”不会被 GMM 吃掉；
    - 只用窄过渡带（腐 2 / 膨 5），边界贴真实轮廓。"""
    HR, WR = raw.shape
    sc = 1100.0 / max(HR, WR)
    w2, h2 = max(64, int(round(WR * sc))), max(64, int(round(HR * sc)))
    P2 = cv2.resize(P.astype(np.uint8), (w2, h2), interpolation=cv2.INTER_AREA)
    r2 = cv2.resize((raw > 0).astype(np.uint8) * 255, (w2, h2), interpolation=cv2.INTER_AREA)
    r2 = (r2 > 127).astype(np.uint8)
    if r2.sum() < 40:
        return raw
    gc = np.where(r2 > 0, cv2.GC_PR_FGD, cv2.GC_PR_BGD).astype(np.uint8)
    gc[cv2.erode(r2, np.ones((3, 3), np.uint8)) > 0] = cv2.GC_FGD
    gc[cv2.dilate(r2, np.ones((5, 5), np.uint8)) == 0] = cv2.GC_BGD
    gc[:2] = cv2.GC_BGD; gc[-2:] = cv2.GC_BGD; gc[:, :2] = cv2.GC_BGD; gc[:, -2:] = cv2.GC_BGD
    if int((gc == cv2.GC_FGD).sum()) < 20 or int((gc == cv2.GC_BGD).sum()) < 20:
        return raw
    try:
        bgm = np.zeros((1, 65), np.float64); fgm = np.zeros((1, 65), np.float64)
        cv2.grabCut(cv2.cvtColor(P2, cv2.COLOR_RGB2BGR), gc, None, bgm, fgm, iters, cv2.GC_INIT_WITH_MASK)
    except Exception:
        return raw
    out = np.where((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    out = cv2.resize(out, (WR, HR), interpolation=cv2.INTER_LINEAR)
    out = (out > 127).astype(np.uint8)
    if out.mean() < 0.008:
        return raw
    return out


def _keep_cluster(m: np.ndarray, pad: float = 0.08) -> np.ndarray:
    """只保留“最大连通块邻域内”的部分：远离主体的碎片（山脊误检、云点）一律丢弃"""
    n, lab, st, _ = cv2.connectedComponentsWithStats(m.astype(np.uint8), 8)
    if n <= 2:
        return m
    areas = st[1:, cv2.CC_STAT_AREA]
    main = 1 + int(np.argmax(areas))
    x, y, w, h = st[main, 0], st[main, 1], st[main, 2], st[main, 3]
    H, W = m.shape
    if w > 0.85 * W:                 # 主体本身横贯画面（天际线等）→ 不做邻域剪裁
        return m
    px, py = int(w * pad), int(h * pad)
    x0, y0 = max(0, x - px), max(0, y - py)
    x1, y1 = min(W, x + w + px), min(H, y + h + py)
    keep = np.zeros_like(m)
    keep[y0:y1, x0:x1] = m[y0:y1, x0:x1]
    # 贴底的连通块属于同一组“落地主体”（例如天际线的各栋楼）→ 找回
    kb = max(2, min(H, W) // 40)
    for i in range(1, n):
        if i == main:
            continue
        if st[i, cv2.CC_STAT_TOP] + st[i, cv2.CC_STAT_HEIGHT] >= H - kb:
            keep[lab == i] = m[lab == i]
    return keep

def _drop_bands(m: np.ndarray, W: int, H: int) -> np.ndarray:
    """去掉“横贯画面的背景带”：既处理独立横带块，也按行切断与主体相连的云带/山脊带"""
    out = m.copy()
    kb = max(2, min(H, W) // 40)
    # (a) 独立横带连通块（不沾底）
    n, lab, st, _ = cv2.connectedComponentsWithStats(m.astype(np.uint8), 8)
    areas = st[1:, cv2.CC_STAT_AREA] if n > 1 else np.array([1])
    main_lab = 1 + int(np.argmax(areas)) if n > 1 else 0
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if i == main_lab:
            continue
        if w > 0.85 * W and h < 0.55 * H and (y + h) < (H - kb):
            out[lab == i] = 0
    # (b) 按行切：整行几乎被占满、且不贴底、且连续高度 <55%H 的行段 = 背景带
    rowcov = (out > 0).mean(axis=1)
    rowcov[int(0.45 * H):] = 0            # 只裁画面上部的横带：主体宽部通常在下方
    band = (rowcov > 0.80).astype(np.uint8)
    k = max(2, H // 60)
    band = cv2.morphologyEx((band * 255)[:, None], cv2.MORPH_CLOSE, np.ones((k, 1), np.uint8))[:, 0] > 0
    i = 0
    while i < H:
        if band[i]:
            j = i
            while j < H and band[j]:
                j += 1
            if (j - i) < 0.55 * H and j < (H - kb):
                out[i:j] = 0
            i = j
        else:
            i += 1
    return out


def _drop_wrapping(mask: np.ndarray, W: int, H: int) -> np.ndarray:
    """去掉“包住画面”的大块（=背景不是主体）：触上/左/右、不触底、面积>15% 且实心度>0.35"""
    try:
        from scipy import ndimage as _ndi
    except Exception:
        return mask
    lab, n = _ndi.label(mask)
    if n == 0:
        return mask
    main_lab = 1 + int(np.argmax([(lab == i).sum() for i in range(1, n)])) if n > 1 else 0
    k = max(2, min(H, W) // 40)
    touch = {}
    for nm, arr in [("t", lab[:k]), ("l", lab[:, :k]), ("r", lab[:, -k:]), ("b", lab[-k:])]:
        for v in np.unique(arr):
            if v:
                touch.setdefault(int(v), set()).add(nm)
    out = mask.copy()
    for v, names in touch.items():
        # 只有“触及上边、不触底”的大实心块才算环绕背景；
        # 只贴左右的是横跨画面的主体（例如 3:4 裁切下两侧出框的自行车）
        if "b" in names or "t" not in names or v == main_lab:
            continue
        sel = (lab == v)
        sz = int(sel.sum())
        if sz < 0.15 * mask.size:
            continue
        ys, xs = np.where(sel)
        bb = (ys.max() - ys.min() + 1) * (xs.max() - xs.min() + 1)
        if bb and sz / bb > 0.35:                 # 实心大块 → 多半是背景
            out[sel] = False
    return out


def smooth_mask(m, close_k=9, blur=2.0, up=4):
    """4x 上采样 → 平滑 → 阈值 → 面积降采样 = 抗锯齿的“平滑硬边”"""
    m8 = ((m > 0).astype(np.uint8)) * 255
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_k, close_k))
    m8 = cv2.morphologyEx(m8, cv2.MORPH_CLOSE, k)
    H, W = m8.shape
    big = cv2.resize(m8, (W * up, H * up), interpolation=cv2.INTER_NEAREST)
    big = cv2.GaussianBlur(big, (0, 0), blur * up)
    big = (big > 127).astype(np.uint8) * 255
    return cv2.resize(big, (W, H), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0


def solid(H, W, c):
    a = np.zeros((H, W, 3), np.float32); a[...] = np.array(c, np.float32); return a


def over(base, c, m):
    return base * (1 - m[..., None]) + np.array(c, np.float32) * m[..., None]


def tile_to(a, H, W):
    th, tw = a.shape
    return np.tile(a, (H // th + 1, W // tw + 1))[:H, :W]


def fit(arr, w, h, bg=(255, 255, 255)):
    im = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).resize((w, h), Image.LANCZOS)
    return np.asarray(im).astype(np.float32)


# ---------------------------------------------------------------- 底盘与元素
class Plate:
    """清理过的底盘 + 元素蒙版。三种自动模式 + 两种手控入口，失败自动降级。

    mode:
      auto (默认) 依次尝试: blue(夜空蓝天) -> border(边缘连通平滑背景) -> saliency(与边框色差)
      poly   手给主体多边形:  --poly "x1,y1;x2,y2;..." (原图像素坐标, 自动换算)
      mask   手给前景蒙版:    --mask path.png  (白=主体)
    """

    def __init__(self, src: Path, work: int = 1152, mode: str = "auto", poly: str | None = None,
                 mask: str | None = None, aspect: str = "3:4"):
        im = Image.open(src).convert("RGB")
        # 保持比例：先按目标画幅中心裁切，再缩放（不拉伸）
        _t = {"3:4": (3, 4), "4:3": (4, 3), "1:1": (1, 1)}.get(aspect, (3, 4))
        W0, H0 = im.size
        if W0 / H0 > _t[0] / _t[1]:                        # 原图太宽 → 裁左右
            nw = int(round(H0 * _t[0] / _t[1])); left = (W0 - nw) // 2
            im = im.crop((left, 0, left + nw, H0))
        elif W0 / H0 < _t[0] / _t[1]:                      # 原图太高 → 裁上下
            nh = int(round(W0 * _t[1] / _t[0])); top = (H0 - nh) // 2
            im = im.crop((0, top, W0, top + nh))
        W0, H0 = im.size
        ar = {"3:4": 3 / 4, "4:3": 4 / 3, "1:1": 1.0}.get(aspect, 3 / 4)   # 宽/高
        if ar >= 1:
            WR, HR = int(work * ar), work
        else:
            WR, HR = work, int(work / ar)
        self.small = im.resize((WR, HR), Image.LANCZOS)
        self.W, self.H = WR, HR
        P = np.asarray(self.small).astype(np.float32)
        L = P @ LW
        Lsm = GB(cv2.bilateralFilter(np.clip(L, 0, 255).astype(np.uint8), 15, 20, 15).astype(np.float32), 1.0)
        self.L, self.Lsm = L, Lsm
        self.orig = P.astype(np.uint8)
        # ---- 焦点光源（严格校验；失败返回 None）----
        self.focal = self._detect_focal(P, L)
        sx, sy = WR / W0, HR / H0
        if self.focal:
            cx, cy, mr = self.focal
        else:                                      # 无焦点时给一个“安全锚点”，不参与构图
            cx, cy, mr = WR * 0.5, HR * 0.12, min(WR, HR) * 0.04
        self.moon_c, self.moon_r = (cx, cy), mr
        self.has_focal = self.focal is not None     # 没有真实光源时，禁止画假月亮
        yy, xx = np.mgrid[0:HR, 0:WR].astype(np.float32)
        moon_disc = ((np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) < mr).astype(np.uint8) * 255) if self.focal else np.zeros((HR, WR), np.uint8)
        moon_disc = cv2.medianBlur(moon_disc, 5) // 255
        # ---- 主体蒙版：按模式/降级链 ----
        if mask:
            mm = Image.open(mask).convert("L").resize((WR, HR), Image.LANCZOS)
            raw = (np.asarray(mm) > 127).astype(np.uint8)
            used = "mask"
        elif poly or mode == "poly":
            raw = np.zeros((HR, WR), np.uint8)
            pts = []
            for pair in (poly or "").split(";"):
                if "," in pair:
                    a, b = pair.split(",")[:2]
                    pts.append([int(float(a) * sx), int(float(b) * sy)])
            if len(pts) >= 3:
                cv2.fillPoly(raw, [np.array(pts, np.int32)], 1)
                used = "poly"
            else:
                raw = self._auto_blue(P, L, moon_disc); used = "blue(poly 无效, 回退)"
        else:
            # 前置守卫：只有“天空确实是蓝的”才允许用蓝通道法
            rr = float(np.median(P[0:max(8, HR // 12), 0:max(8, WR // 12), 0]))
            bb = float(np.median(P[0:max(8, HR // 12), 0:max(8, WR // 12), 2]))
            blue_sky = (bb - rr) > 25
            if blue_sky:
                raw = self._auto_blue(P, L, moon_disc); used = "blue"
                cov = raw.mean()
                # 语义检查：真主体不该占满画框边缘（否则多半是把背景当成了主体）
                ring = np.zeros_like(raw); k = max(2, min(HR, WR) // 40)
                ring[:k] = 1; ring[-k:] = 1; ring[:, :k] = 1; ring[:, -k:] = 1
                bf = float((raw * ring).sum() / max(ring.sum(), 1))
                if bf > 0.35:
                    raw, used, cov = None, "blue(reject:border)", 0.0
            else:
                raw, used, cov = None, "", 0.0
            cands = []
            if raw is not None:                                   # blue 候选（已过边框守卫）
                cands.append((raw, "blue"))
            cands.append((self._border_smooth(P, L), "border"))
            cands.append((self._saliency(P, L), "saliency"))
            best, best_sc, best_name = None, -99.0, "full"
            for m0, nm0 in cands:
                m0 = _drop_wrapping(_drop_bands(m0.astype(np.uint8), WR, HR), WR, HR)
                sc0 = _mask_score(m0, WR, HR, L)
                if sc0 > best_sc:
                    best, best_sc, best_name = m0, sc0, nm0
            if best is None or best_sc <= -8:                      # 全都不像主体 → 整幅
                best, best_name = np.ones((HR, WR), np.uint8), "full"
            best = _bbox_restrict(best)                 # 排除远处的背景带，再交给 GrabCut
            ref = _refine_mask(P, best)                 # 物体级分割（高分辨率 + 掩版先验，保留负空间）
            cm = float(best.mean())
            if cm > 0 and 0.70 * cm <= float(ref.mean()) <= 1.35 * cm:
                best = ref
            # 精修输出即为最终主体：不再做任何“削减类”后处理（它们会啃掉细结构与负空间）
            raw, used = best, best_name
        raw = cv2.morphologyEx(raw.astype(np.uint8) * 255, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)) // 255
        bld = clean_mask((raw & (1 - moon_disc)).astype(np.uint8), int(WR * HR * 0.004), 9)
        # 堵漏：把被主体围住的“内部空洞”并入主体。种子必须取自真的背景像素，
        # 否则掩版噪声占住角落时会把整幅背景误当空洞（曾经导致 bld=1.0）
        # 阴影剔除：地面投影与车身一样暗，靠“纹理+形状”区分——
        # 投影是光滑（低梯度）且“宽而扁”的连通块；车身部件紧凑且有边缘。
        if (bld > 0).sum() > 50:
            gl2 = cv2.GaussianBlur(CB(L), (0, 0), 3.0)
            gx3 = cv2.Sobel(gl2, cv2.CV_32F, 1, 0, 3); gy3 = cv2.Sobel(gl2, cv2.CV_32F, 0, 1, 3)
            gr2 = cv2.GaussianBlur(np.sqrt(gx3 * gx3 + gy3 * gy3), (0, 0), 3.0)
            inside2 = (bld > 0)
            g_thr2 = float(np.percentile(gr2[inside2], 45)) if inside2.any() else 0.0
            cand2 = (inside2 & (gr2 < g_thr2)).astype(np.uint8)
            n_sh2, lab_sh2, st_sh2, _ = cv2.connectedComponentsWithStats(cand2, 8)
            for i4 in range(1, n_sh2):
                w4, h4, a4 = st_sh2[i4, 2], st_sh2[i4, 3], st_sh2[i4, 4]
                if w4 > 0.33 * WR and h4 < 0.45 * HR and a4 > 0.003 * WR * HR:
                    bld[lab_sh2 == i4] = 0
        # 堵漏：被围住的光滑区按颜色判回（更接近主体→填；更接近背景→留空）
        seed = None
        for yy in (0, HR // 8, HR // 4, HR // 2, 3 * HR // 4, HR - 1):
            for xx in (0, WR // 8, WR // 4, WR // 2, 3 * WR // 4, WR - 1):
                if bld[yy, xx] == 0:
                    seed = (int(xx), int(yy)); break
            if seed:
                break
        if seed and (bld > 0).sum() > 50:
            ff = bld.copy(); ffm = np.zeros((HR + 2, WR + 2), np.uint8)
            cv2.floodFill(ff, ffm, seed, 2)
            enclosed = ((bld == 0) & (ff != 2)).astype(np.uint8)
            if 0 < enclosed.sum() < WR * HR * 0.35:
                subj_col = np.median(P[bld > 0], axis=0)
                kb2 = max(2, min(HR, WR) // 40)
                ring2 = np.zeros((HR, WR), np.uint8)
                ring2[:kb2] = 1; ring2[-kb2:] = 1; ring2[:, :kb2] = 1; ring2[:, -kb2:] = 1
                bg_col = np.median(P[ring2 > 0], axis=0)
                n2, lab2, st2, _ = cv2.connectedComponentsWithStats(enclosed, 8)
                addm = np.zeros_like(enclosed)
                for i2 in range(1, n2):
                    hole = (lab2 == i2)
                    hc = np.median(P[hole], axis=0)
                    if float(np.linalg.norm(hc - subj_col)) < float(np.linalg.norm(hc - bg_col)):
                        addm[hole] = 1
                if addm.sum() < WR * HR * 0.05:
                    bld = clean_mask((np.clip(bld + addm, 0, 1) * (1 - moon_disc)).astype(np.uint8),
                                     int(WR * HR * 0.003), 7)
        if bld.mean() < 0.005:                                 # 仍失败：整幅当主体，风格仍可用
            bld = (1 - moon_disc).astype(np.uint8); used += "->full"
        self.used_mode = used
        # 可读性简化：删掉比“格子成品尺寸”还细的结构（如轮圈辐条）。
        # 不做这一步，缩到 384px 格子时细线会糊成实心块（"整个车糊掉"的观感来源）。
        kk = max(5, int(round(min(HR, WR) / 120.0)))
        kk += kk % 2 == 0
        op = cv2.morphologyEx(bld.astype(np.uint8) * 255, cv2.MORPH_OPEN,
                              cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kk, kk))) // 255
        if op.mean() >= 0.6 * bld.mean():   # 简化别把主体吃没
            bld = op
        self.bld = smooth_mask(bld, 3, 1.1)
        # ---- 开口 ----
        dark_in = ((Lsm < np.percentile(Lsm, 25)) & (self.bld > 0.5)).astype(np.uint8)
        n_, lab, st, _ = cv2.connectedComponentsWithStats(dark_in, 8)
        op = np.zeros((HR, WR), np.uint8)
        for i in range(1, n_):
            if 120 < st[i, cv2.CC_STAT_AREA] < WR * HR * 0.03:
                op[lab == i] = 1
        self.open = smooth_mask(clean_mask(op, 200, 7), 7, 1.6)
        # ---- 亮面 / 暗面 ----
        inside = self.bld > 0.5
        thr = float(np.percentile(Lsm[inside], 65)) if inside.sum() > 500 else 140.0
        lit_idx = cv2.medianBlur(np.digitize(Lsm, [thr]).astype(np.uint8), 11)
        face = np.clip(self.bld - self.open, 0, 1)
        self.lit = smooth_mask(clean_mask(((face > 0.5) * lit_idx).astype(np.uint8), 400, 11), 9, 2.0)
        self.shad = np.clip(face - self.lit, 0, 1)
        # ---- 月亮亮/食面 ----
        self.moon = smooth_mask(moon_disc, 5, 1.0)
        msrc = (moon_disc > 0)
        mthr = np.percentile(Lsm[msrc], 55) if msrc.sum() > 50 else 62
        self.m_lit = smooth_mask(clean_mask((msrc & (Lsm > mthr)).astype(np.uint8), 100, 5), 7, 1.4)
        self.sky = np.clip(1 - np.clip(self.bld + self.moon, 0, 1), 0, 1)
        kb = (self.bld > 0.5).astype(np.uint8) * 255
        key = GB(cv2.dilate(kb, np.ones((5, 5), np.uint8)) - cv2.erode(kb, np.ones((5, 5), np.uint8)), 0.8) / 255.0
        self.key = np.clip(key, 0, 1) * (1 - self.moon)
        self.plate = self.orig

    # ---------- 三种自动策略 ----------
    @staticmethod
    def _detect_focal(P, L):
        """严格：小而圆、明显亮于周围，否则 None"""
        r = P[..., 0]; g = P[..., 1]; b = P[..., 2]
        # 强暖色团块（月亮/太阳/暖色光源）：R 明显高于 B 与 G
        m = ((r > 100) & (r > b * 1.25) & (r > g * 1.25)).astype(np.uint8)
        if m.sum() < 40:
            return None
        rmin = 0.025 * min(P.shape[0], P.shape[1])
        n, lab, st, cent = cv2.connectedComponentsWithStats(m, 8)
        best = None
        for i in range(1, n):
            x, y, w, h, a = st[i]
            if a < 40 or a > 0.02 * P.shape[0] * P.shape[1]:
                continue
            fill = a / float(w * h + 1e-6)
            ar = w / float(h + 1e-6)
            if a < math.pi * rmin * rmin * 0.5:
                continue
            if fill < 0.35 or not (0.5 < ar < 2.0):
                continue
            cx, cy = cent[i]
            rr = max(w, h) * 0.5
            if rr < rmin:
                continue
            inner = L[int(cy - rr):int(cy + rr), int(cx - rr):int(cx + rr)]
            ring_r = rr * 2.2
            ring = L[max(0, int(cy - ring_r)):int(cy + ring_r), max(0, int(cx - ring_r)):int(cx + ring_r)]
            if inner.size and ring.size and inner.mean() > ring.mean() + 25:
                best = (float(cx), float(cy), float(rr))
        return best

    @staticmethod
    def _auto_blue(P, L, moon_disc):
        HR, WR = L.shape
        sky_ref = float(np.median(P[0:max(8, HR // 12), 0:max(8, WR // 12), 2]))
        cands = [(0.80, 0.95, 0.62, 0.72), (0.02, 0.18, 0.62, 0.72), (0.40, 0.62, 0.72, 0.82), (0.80, 0.98, 0.45, 0.58)]
        dark_ref = min(float(np.median(P[int(HR * y0):int(HR * y1), int(WR * x0):int(WR * x1), 2])) for x0, x1, y0, y1 in cands)
        thrB = (sky_ref + dark_ref) / 2 if sky_ref > dark_ref else sky_ref * 0.85
        notsky = cv2.morphologyEx((((P[..., 2] < thrB) | (L > 120)).astype(np.uint8) * 255), cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8)) // 255
        return (notsky & (1 - moon_disc)).astype(np.uint8)

    @staticmethod
    def _border_smooth(P, L):
        """从四边向内泛洪“低梯度”像素 = 背景；其余为主体（适合白天/纯色背景）"""
        HR, WR = L.shape
        grad = np.sqrt(cv2.Sobel(CB(GB(L, 3.0)), cv2.CV_32F, 1, 0, 3) ** 2 + cv2.Sobel(CB(GB(L, 3.0)), cv2.CV_32F, 0, 1, 3) ** 2)
        gthr = max(6.0, float(np.percentile(grad, 72)))
        passable = ((grad < gthr).astype(np.uint8) * 255)
        passable = cv2.morphologyEx(passable, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        ffm = np.zeros((HR + 2, WR + 2), np.uint8)
        for x in range(2, WR, max(4, WR // 48)):
            if passable[0, x]: cv2.floodFill(passable, ffm, (x, 0), 128)
            if passable[HR - 1, x]: cv2.floodFill(passable, ffm, (x, HR - 1), 128)
        for y in range(2, HR, max(4, HR // 48)):
            if passable[y, 0]: cv2.floodFill(passable, ffm, (0, y), 128)
            if passable[y, WR - 1]: cv2.floodFill(passable, ffm, (WR - 1, y), 128)
        bg = (passable == 128)
        return (~bg).astype(np.uint8)

    @staticmethod
    def _saliency(P, L):
        """与画面边框色差大者为前景（最后的兜底）"""
        HR, WR = L.shape
        k = max(4, min(HR, WR) // 14)
        border = np.concatenate([P[:k].reshape(-1, 3), P[-k:].reshape(-1, 3), P[:, :k].reshape(-1, 3), P[:, -k:].reshape(-1, 3)])
        mean = border.mean(0)
        d = np.sqrt(((P - mean[None, None, :]) ** 2).sum(-1))
        thr = float(np.percentile(d, 68))
        fg = (d > thr).astype(np.uint8)
        fg = cv2.morphologyEx(fg * 255, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8)) // 255
        return fg

    def save_plate(self, path: Path):
        Image.fromarray(self.plate).save(path)

    def describe(self):
        return dict(size=(self.W, self.H), mode=self.used_mode,
                    focal=None if not self.focal else dict(center=[round(v, 1) for v in self.focal[:2]], radius=round(self.focal[2], 1)),
                    coverage=dict(bld=round(float(self.bld.mean()), 3), sky=round(float(self.sky.mean()), 3),
                                  lit=round(float(self.lit.mean()), 3), shad=round(float(self.shad.mean()), 3),
                                  open=round(float(self.open.mean()), 4)))


# ---------------------------------------------------------------- 配色
PALETTES = {
    "A": dict(sky=(214, 42, 44), bld=(245, 238, 224), ink=(24, 22, 26), moon=(240, 232, 216), dark=(178, 26, 36)),
    "B": dict(sky=(16, 34, 74), bld=(238, 230, 208), ink=(12, 14, 20), moon=(232, 96, 40), dark=(70, 92, 140)),
    "C": dict(sky=(18, 118, 118), bld=(246, 244, 238), ink=(26, 26, 32), moon=(230, 60, 150), dark=(120, 168, 200)),
    "D": dict(sky=(28, 24, 60), bld=(245, 235, 210), ink=(14, 12, 24), moon=(198, 160, 74), dark=(96, 72, 150)),
    "E": dict(sky=(238, 232, 216), bld=(32, 34, 44), ink=(20, 20, 24), moon=(200, 60, 48), dark=(180, 170, 150)),
}


# ---------------------------------------------------------------- 风格库
def st_hardedge(P: Plate, pal, k):
    m = (P.bld > 0.5).astype(np.float32)
    o = (P.open > 0.5).astype(np.float32)
    i = solid(P.H, P.W, pal["bld"] if k == 2 else pal["sky"])
    i = over(i, pal["sky"] if k == 2 else pal["bld"], m)
    i = over(i, pal["ink"], o * m)
    i = over(i, pal["ink"], np.clip(P.key * m, 0, 1) * 0.5)       # 硬边轮廓线
    im = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    if P.has_focal:
        d.ellipse([P.moon_c[0] * 0.2 + P.W * 0.55, P.H * 0.06, P.moon_c[0] * 0.2 + P.W * 0.55 + 180, P.H * 0.06 + 180],
                  outline=pal["bld"], width=10)
    return np.asarray(im).astype(np.float32)


def st_negative(P, pal, k):
    i = 255.0 - P.orig.astype(np.float32)
    t = np.clip((i @ LW) / 255.0, 0, 1)[..., None]
    return np.array(pal["sky"], np.float32) * (1 - t) + np.array(pal["moon"], np.float32) * t


def st_halftone(P: Plate, pal, k):
    cell = [15, 10, 22][k - 1]
    inv = float(np.median(P.L)) < 110          # 暗图：用反相网点（在暗处落点）
    src = (255.0 - P.L) if inv else P.L
    sh = cv2.resize(src, (max(1, P.W // cell), max(1, P.H // cell)), interpolation=cv2.INTER_AREA)
    i = solid(P.H, P.W, pal["sky"])
    im = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    for gy in range(sh.shape[0]):
        for gx in range(sh.shape[1]):
            v = sh[gy, gx] / 255.0
            if v < 0.2: continue
            cx, cy = gx * cell + cell // 2, gy * cell + cell // 2
            r = (cell * 0.66) * min(1.0, (v - 0.18) * 1.5)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(pal["dark"] if inv else pal["bld"]) if v > 0.55 else (pal["bld"] if inv else pal["dark"]))
    if P.has_focal:
        d.ellipse([P.moon_c[0] - P.moon_r, P.moon_c[1] - P.moon_r, P.moon_c[0] + P.moon_r, P.moon_c[1] + P.moon_r], fill=pal["moon"])
    return np.asarray(im).astype(np.float32)


def st_mirror(P: Plate, pal, k):
    h = P.orig if k != 3 else np.clip(255 - P.orig, 0, 255).astype(np.uint8)
    mir = np.concatenate([h, h[:, ::-1]], 1) if k != 2 else np.concatenate([P.orig, P.orig[::-1]], 0)
    return fit(mir, P.W, P.H)


def st_strips(P: Plate, pal, k):
    """拼接：整幅照片切成竖条，隔条微微错位 + 细缝——拼贴/重排感，车身保持完整可辨"""
    n = [12, 18, 8][k - 1]
    xs = np.linspace(0, P.W, n + 1).astype(int)
    i = P.orig.astype(np.float32).copy()
    rng = np.random.default_rng(5 + k)
    amp = P.H * [0.020, 0.035, 0.012][k - 1]
    for b in range(n):
        x0, x1 = xs[b], xs[b + 1]
        if x1 - x0 < 2:
            continue
        if b % 2 == 0:
            i[:, x0:x1] = np.roll(P.orig[:, x0:x1].astype(np.float32), int(round(rng.normal(0, amp))), axis=0)
    for x in xs[1:-1]:
        i[:, x] = i[:, x] * 0.55 + np.array(pal["ink"], np.float32) * 0.45
    return np.clip(i, 0, 255)


def st_engraving(P: Plate, pal, k):
    H, W = P.H, P.W
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    dark = np.clip((150.0 - P.Lsm) / 150.0, 0, 1)
    ang = [0.7, 1.35, 0.45][k - 1]
    pat = (np.sin((xx * np.cos(ang) + yy * np.sin(ang)) * 0.75) > 0.45).astype(np.float32)
    pat2 = (np.sin((xx * np.cos(-ang) + yy * np.sin(-ang)) * 0.75) > 0.45).astype(np.float32)
    hatch = np.clip(dark * 1.3, 0, 1) * np.where(dark > 0.5, pat2, pat)
    gx = cv2.Sobel(CB(P.Lsm), cv2.CV_32F, 1, 0, 3); gy = cv2.Sobel(CB(P.Lsm), cv2.CV_32F, 0, 1, 3)
    line = cv2.GaussianBlur((np.sqrt(gx * gx + gy * gy) > 16).astype(np.float32), (0, 0), 0.6)
    ink = np.clip(hatch * 0.9 + line, 0, 1)
    i = solid(H, W, pal["bld"])
    return i * (1 - ink[..., None] * 0.92) + np.array(pal["ink"], np.float32) * ink[..., None] * 0.92


def st_pattern(P: Plate, pal, k):
    """图案（带原作痕迹）：网格上重复小母题，母题的尺寸与颜色取自该处照片明暗"""
    n = [14, 20, 10][k - 1]
    small = cv2.resize(P.orig, (n, n), interpolation=cv2.INTER_AREA).astype(np.float32)
    Ls = cv2.resize(P.L, (n, n), interpolation=cv2.INTER_AREA) / 255.0
    im = Image.fromarray(np.clip(solid(P.H, P.W, pal["bld"]), 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    cw = P.W / n
    for gy in range(n):
        for gx in range(n):
            wgt = 1.0 - float(Ls[gy, gx])
            if wgt < 0.10:
                continue
            cx, cy = (gx + 0.5) * cw, (gy + 0.5) * cw
            r = cw * 0.48 * math.sqrt(max(0.05, wgt))          # 面积 ∝ 暗度 → 格均色贴近原作
            col = tuple(int(v) for v in small[gy, gx])
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=tuple(int(v) for v in pal["ink"]), width=max(1, int(r * 0.18)))
    return np.asarray(im).astype(np.float32)


def st_deco(P: Plate, pal, k):
    rays = [28, 44, 16][k - 1]
    i = solid(P.H, P.W, pal["dark"] if k == 3 else (10, 22, 48))
    im = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    for q in range(rays if P.has_focal else 0):
        a = q * np.pi / (rays / 2)
        d.line([(P.moon_c[0], P.moon_c[1]), (P.moon_c[0] + 1900 * math.cos(a), P.moon_c[1] + 1900 * math.sin(a))],
               fill=pal["moon"], width=6)
    i = np.asarray(im).astype(np.float32)
    i = over(i, pal["bld"], P.bld); i = over(i, pal["moon"], P.moon)
    im = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    for rr in ((2.4, 2.7) if P.has_focal else ()):
        d.ellipse([P.moon_c[0] - P.moon_r * rr, P.moon_c[1] - P.moon_r * rr, P.moon_c[0] + P.moon_r * rr, P.moon_c[1] + P.moon_r * rr],
                  outline=pal["moon"], width=6 if rr < 3 else 2)
    d.rectangle([26, 26, P.W - 26, P.H - 26], outline=pal["moon"], width=6)
    return np.asarray(im).astype(np.float32)


def st_flat(P: Plate, pal, k):
    i = solid(P.H, P.W, pal["sky"])
    # 剪纸投影
    sh = np.roll(np.roll(P.bld, 14, 0), 14, 1)          # 硬投影（去掉软阴影）
    i = over(i, pal["bld"], P.bld); i = over(i, pal["ink"], P.open); i = over(i, pal["moon"], P.moon)
    if k == 3:
        rng = np.random.default_rng(5)
        im = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
        for _ in range(120):
            x, y = float(rng.uniform(20, P.W - 20)), float(rng.uniform(20, P.H * 0.72))
            rr = float(rng.uniform(1.5, 3))
            d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=pal["bld"])
        i = np.asarray(im).astype(np.float32)
    return i


def st_collage(P: Plate, pal, k):
    """拼接：整幅照片打底，再叠几张同片裁块（硬边纸边、无软投影），车身始终可辨"""
    i = P.orig.astype(np.float32) * 0.94
    rng = np.random.default_rng(7 + k)
    ys, xs_ = np.where(P.bld > 0.5)
    for _ in range([3, 5, 2][k - 1]):
        w = int(P.W * rng.uniform(0.30, 0.55)); h = int(P.H * rng.uniform(0.20, 0.40))
        if len(ys):
            cy, cx = int(rng.choice(ys)), int(rng.choice(xs_))
        else:
            cy, cx = P.H // 2, P.W // 2
        y0 = int(np.clip(cy - h // 2, 0, P.H - h)); x0 = int(np.clip(cx - w // 2, 0, P.W - w))
        patch = P.orig[y0:y0 + h, x0:x0 + w].astype(np.float32)
        ty0 = int(np.clip(y0 + int(rng.normal(0, P.H * 0.02)), 0, P.H - h))
        tx0 = int(np.clip(x0 + int(rng.normal(0, P.W * 0.02)), 0, P.W - w))
        i[ty0:ty0 + h, tx0:tx0 + w] = patch
        e = np.array([250, 248, 242], np.float32)
        i[ty0:ty0 + 2, tx0:tx0 + w] = e; i[ty0 + h - 2:ty0 + h, tx0:tx0 + w] = e
        i[ty0:ty0 + h, tx0:tx0 + 2] = e; i[ty0:ty0 + h, tx0 + w - 2:tx0 + w] = e
    return np.clip(i, 0, 255)


def st_pixel(P: Plate, pal, k):
    """真·像素画：降采样 → 中位切分调色板 + Floyd–Steinberg 抖动 → 最近邻放大 → 主体描边"""
    lw = [92, 132, 60][k - 1]
    sc = max(1, int(round(P.W / lw)))
    w2, h2 = max(8, P.W // sc), max(8, P.H // sc)
    small = cv2.resize(P.orig, (w2, h2), interpolation=cv2.INTER_AREA)
    ncol = [22, 30, 14][k - 1]
    im = Image.fromarray(small.astype(np.uint8)).quantize(colors=ncol, method=Image.MEDIANCUT,
                                                          dither=Image.FLOYDSTEINBERG).convert("RGB")
    q = np.asarray(im).astype(np.float32)
    ink = np.array(pal["ink"], np.float32)
    b = (cv2.resize(P.bld, (w2, h2), interpolation=cv2.INTER_AREA) > 0.45).astype(np.uint8)
    edge = (b & ~(cv2.erode(b, np.ones((3, 3), np.uint8)) > 0)).astype(bool)
    q[edge] = ink                                          # 像素画惯例：主体描一圈墨线
    core = b.astype(bool) & ~edge
    q[core] = q[core] * 0.84 + ink * 0.16                  # 主体略向墨色收敛，缩小后仍可辨
    return cv2.resize(q, (P.W, P.H), interpolation=cv2.INTER_NEAREST)



def st_lineart(P: Plate, pal, k):
    """轮廓线稿：只画主体外轮廓 + 内部细节线（缩到小尺寸依然分得清结构）"""
    i = solid(P.H, P.W, pal["sky"])
    wgt = [1.0, 1.6, 0.7][k - 1]
    key = np.clip(P.key * (P.bld > 0.5), 0, 1) * wgt
    i = over(i, pal["ink"], np.clip(key, 0, 1))
    i = over(i, pal["ink"], np.clip(P.open, 0, 1) * 0.85 * wgt)
    if P.has_focal:
        im = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
        rr = P.moon_r * 1.15
        d.ellipse([P.moon_c[0] - rr, P.moon_c[1] - rr, P.moon_c[0] + rr, P.moon_c[1] + rr], outline=pal["moon"], width=max(2, int(6 * wgt)))
        i = np.asarray(im).astype(np.float32)
    return i

def st_glitch(P: Plate, pal, k):
    """故障化：RGB 通道偏移 + 少量横向块错位 + 轻微扫描线。保留照片内容，车身完整可辨"""
    i = P.orig.astype(np.float32).copy()
    H, W = P.H, P.W
    rng = np.random.default_rng(11 + k)
    sh = [3, 6, 2][k - 1]
    i[..., 0] = np.roll(i[..., 0], sh, axis=1)
    i[..., 2] = np.roll(i[..., 2], -sh, axis=1)
    for _ in range([7, 11, 5][k - 1]):
        y0 = int(H * rng.uniform(0.05, 0.92)); h = int(H * rng.uniform(0.01, 0.05))
        off = int(rng.normal(0, W * 0.012))
        i[y0:min(H, y0 + h)] = np.roll(i[y0:min(H, y0 + h)], off, axis=1)
    i[::[7, 5, 10][k - 1]] *= 0.9
    return np.clip(i, 0, 255)



def st_cross(P: Plate, pal, k):
    """十字绣：亚麻底 + 每格绣一个 X（色=该格均色）。照片以针法再现，主体完整"""
    cell = [9, 6, 13][k - 1]
    nw, nh = max(8, P.W // cell), max(8, P.H // cell)
    small = cv2.resize(P.orig, (nw, nh), interpolation=cv2.INTER_AREA)
    try:
        _dth = Image.Dither.NONE
    except Exception:
        _dth = 0
    q = Image.fromarray(small.astype(np.uint8)).quantize(colors=[28, 40, 18][k - 1],
                                                        method=Image.MEDIANCUT, dither=_dth).convert("RGB")
    cols = np.asarray(q).astype(np.float32)
    out = np.zeros((P.H, P.W, 3), np.float32) + np.array([237, 232, 219], np.float32)   # 亚麻
    for gy in range(0, P.H, cell):
        out[gy:gy + 1, :] *= 0.965
    for gx in range(0, P.W, cell):
        out[:, gx:gx + 1] *= 0.965
    th = max(2, int(round(cell * 0.40)))
    for cy in range(nh):
        for cx in range(nw):
            c = tuple(int(v) for v in cols[cy, cx])
            x0, y0 = cx * cell, cy * cell
            cv2.line(out, (x0 + 2, y0 + 2), (x0 + cell - 2, y0 + cell - 2), c, th, cv2.LINE_AA)
            cv2.line(out, (x0 + cell - 2, y0 + 2), (x0 + 2, y0 + cell - 2), c, th, cv2.LINE_AA)
    return np.clip(out, 0, 255)


def st_quilt(P: Plate, pal, k):
    """拼布：照片切成拼布块 + 虚线缝线，每块轻微色调抖动（不旋转，主体保持完整）"""
    nx, ny = [3, 4, 2][k - 1], [4, 6, 3][k - 1]
    i = P.orig.astype(np.float32).copy()
    rng = np.random.default_rng(21 + k)
    for b in range(ny):
        for a in range(nx):
            y0, y1 = int(P.H * b / ny), int(P.H * (b + 1) / ny)
            x0, x1 = int(P.W * a / nx), int(P.W * (a + 1) / nx)
            i[y0:y1, x0:x1] = np.clip(i[y0:y1, x0:x1] * (1.0 + rng.normal(0, 0.05)), 0, 255)
    im = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    st = (252, 248, 238)
    for a in range(1, nx):
        x = int(P.W * a / nx)
        for y in range(0, P.H, 14):
            d.line([x, y, x, y + 8], fill=st, width=3)
    for b in range(1, ny):
        y = int(P.H * b / ny)
        for x in range(0, P.W, 14):
            d.line([x, y, x + 8, y], fill=st, width=3)
    return np.asarray(im).astype(np.float32)


def st_pixelsort(P: Plate, pal, k):
    """像素排序故障：列内非主体区段按亮度排序 → 背景被拉成光带，主体完整不动"""
    i = P.orig.astype(np.float32).copy()
    L = P.L
    m = (P.bld > 0.5)
    thr = float(np.percentile(L, [8, 20, 3][k - 1]))       # 阈值低 = 背景几乎全部拉成光带（强故障感）
    H, W = L.shape
    for x in range(W):
        colL, mcol = L[:, x], m[:, x]
        y = 0
        while y < H:
            if colL[y] < thr or mcol[y]:
                y += 1; continue
            y0 = y
            while y < H and colL[y] >= thr and not mcol[y]:
                y += 1
            if y - y0 > 3:
                order = np.argsort(colL[y0:y]) if (x % 2 == 0) else np.argsort(-colL[y0:y])
                i[y0:y, x] = i[y0:y, x][order]
    return np.clip(i, 0, 255)


def st_mosaic(P: Plate, pal, k):
    """马赛克：小方砖取格均色 + 缝隙，砖面轻微色差（照片的"砖化"，主体完整）"""
    cell = [14, 20, 10][k - 1]
    gut = max(1, cell // 7)
    nw, nh = max(6, P.W // cell), max(6, P.H // cell)
    small = cv2.resize(P.orig, (nw, nh), interpolation=cv2.INTER_AREA).astype(np.float32)
    rng = np.random.default_rng(31 + k)
    out = np.zeros((P.H, P.W, 3), np.float32) + np.array([228, 224, 214], np.float32)
    for cy in range(nh):
        for cx in range(nw):
            y0, x0 = cy * cell + gut, cx * cell + gut
            y1, x1 = min(P.H, (cy + 1) * cell), min(P.W, (cx + 1) * cell)
            if y1 <= y0 or x1 <= x0:
                continue
            c = small[cy, cx] * (1.0 + rng.normal(0, 0.035))
            out[y0:y1, x0:x1] = np.clip(c, 0, 255)
    return np.clip(out, 0, 255)


def st_dotmatrix(P: Plate, pal, k):
    """点阵印刷：每格按暗度画一个圆点（暗→大点），纸张底，颜色取格均色"""
    cell = [8, 6, 11][k - 1]
    nw, nh = max(8, P.W // cell), max(8, P.H // cell)
    small = cv2.resize(P.orig, (nw, nh), interpolation=cv2.INTER_AREA).astype(np.float32)
    lum = cv2.resize(P.L, (nw, nh), interpolation=cv2.INTER_AREA) / 255.0
    out = np.zeros((P.H, P.W, 3), np.float32) + np.array([246, 243, 234], np.float32)
    rmax = cell * 0.60
    for cy in range(nh):
        for cx in range(nw):
            v = 1.0 - float(lum[cy, cx])
            if v <= 0.05:
                continue
            c0 = (int(round((cx + 0.5) * cell)), int(round((cy + 0.5) * cell)))
            cv2.circle(out, c0, max(1, int(round(rmax * math.sqrt(v)))), tuple(float(v2) for v2 in small[cy, cx]), -1, cv2.LINE_AA)
    return np.clip(out, 0, 255)


def st_film(P: Plate, pal, k):
    """胶片接触印相：整幅照片作片基 + 分格白线 + 左右齿孔与片边（照片不打散）"""
    nx, ny = [3, 4, 2][k - 1], [2, 3, 2][k - 1]
    i = P.orig.astype(np.float32) * 0.93
    im = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    fr = (250, 248, 244)
    for a in range(1, nx):
        x = int(P.W * a / nx); d.line([x, 0, x, P.H], fill=fr, width=max(2, P.W // 500))
    for b in range(1, ny):
        y = int(P.H * b / ny); d.line([0, y, P.W, y], fill=fr, width=max(2, P.H // 500))
    hw, hh = max(5, P.W // 110), max(8, P.H // 60)
    for y in range(int(hh * 0.7), P.H - hh, int(hh * 2.2)):
        for x in (max(2, P.W // 120), P.W - max(2, P.W // 120) - hw):
            d.rectangle([x, y, x + hw, y + hh], fill=fr)
    bw = max(4, P.W // 90)
    d.rectangle([0, 0, bw, P.H], fill=(24, 22, 20))
    d.rectangle([P.W - bw, 0, P.W, P.H], fill=(24, 22, 20))
    return np.asarray(im).astype(np.float32)



# ============================== 抽象主义系列 ==============================
def _rot_rect(cx, cy, w, h, ang):
    a = math.radians(ang); ca, sa = math.cos(a), math.sin(a)
    return [(cx + dx * ca - dy * sa, cy + dx * sa + dy * ca)
            for dx, dy in ((-w/2, -h/2), (w/2, -h/2), (w/2, h/2), (-w/2, h/2))]


def st_bauhaus(P: Plate, pal, k):
    """包豪斯：色块颜色取自画面各区真实均色，圆取最亮团块的位置与大小"""
    Ls = cv2.resize(P.L, (24, 24), interpolation=cv2.INTER_AREA)
    gy, gx = np.unravel_index(int(np.argmax(Ls)), Ls.shape)
    small = cv2.resize(P.orig, (24, 24), interpolation=cv2.INTER_AREA).astype(np.float32)
    ys = np.where((P.bld > 0.5).any(axis=1))[0]
    hy = int(ys.mean()) if len(ys) else int(P.H * 0.55)
    top = np.median(P.orig[:max(4, hy // 2)].reshape(-1, 3), axis=0)
    bot = np.median(P.orig[min(P.H - 1, hy + (P.H - hy) // 2):].reshape(-1, 3), axis=0)
    mid = np.median(P.orig[max(0, hy - int(P.H * 0.06)):hy + max(1, int(P.H * 0.06))].reshape(-1, 3), axis=0)
    im = Image.fromarray(np.clip(solid(P.H, P.W, pal["bld"]), 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    d.rectangle([0, hy, P.W, P.H], fill=tuple(int(v) for v in bot))
    r = P.W * (0.16 + 0.20 * float(Ls[gy, gx]) / 255.0)
    cx, cy = (gx + 0.5) / 24 * P.W, (gy + 0.5) / 24 * P.H
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=tuple(int(v) for v in top))
    d.rectangle([P.W * 0.56, hy - P.H * 0.30, P.W * 0.56 + P.W * 0.37, hy], fill=tuple(int(v) for v in small[12, 12]))
    d.polygon([(P.W * 0.12, P.H * 0.87), (P.W * 0.32, P.H * 0.87), (P.W * 0.22, P.H * 0.63)], fill=tuple(int(v) for v in mid))
    d.rectangle([P.W * 0.62, P.H * 0.10, P.W * 0.62 + P.W * 0.30, P.H * 0.10 + P.H * 0.035], fill=pal["ink"])
    d.rectangle([0, hy - max(2, P.H // 90), P.W, hy + max(2, P.H // 90)], fill=pal["ink"])
    return np.asarray(im).astype(np.float32)


def st_supremat(P: Plate, pal, k):
    """至上主义：方块的位置/大小/颜色取自画面的色彩聚类（构图来自原作）"""
    im = Image.fromarray(np.clip(solid(P.H, P.W, pal["ink"]), 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    small = cv2.resize(P.orig, (56, 56), interpolation=cv2.INTER_AREA).reshape(-1, 3).astype(np.float32)
    nc = [6, 9, 4][k - 1]
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 12, 1.0)
    _, lab, centers = cv2.kmeans(small, nc, None, crit, 2, cv2.KMEANS_PP_CENTERS)
    lab = lab.ravel()
    picks = np.array([pal["moon"], pal["bld"], pal["sky"], pal["dark"], pal["ink"]], np.float32)
    for c in range(nc):
        sel = lab == c
        if sel.sum() < 20:
            continue
        gy, gx = np.divmod(np.where(sel)[0], 56)
        cx, cy = (gx.mean() + 0.5) / 56 * P.W, (gy.mean() + 0.5) / 56 * P.H
        ar = sel.sum() / 3136.0
        w_ = P.W * (0.14 + 1.7 * math.sqrt(max(ar, 0.004)))
        col = tuple(int(v) for v in picks[int(np.argmin(np.linalg.norm(picks - centers[c].astype(np.float32), axis=1)))])
        d.polygon(_rot_rect(cx, cy, w_, w_ * 0.42, float((c - nc / 2) * 12)), fill=col)
    return np.asarray(im).astype(np.float32)


def st_destijl(P: Plate, pal, k):
    """风格派：格线取画面横竖梯度峰值；每格填该区照片均色就近的调色板色（构图来自原作）"""
    gx = np.abs(cv2.Sobel(CB(P.L), cv2.CV_32F, 1, 0, 3)).mean(axis=0)
    gy = np.abs(cv2.Sobel(CB(P.L), cv2.CV_32F, 0, 1, 3)).mean(axis=1)
    def peaks(prof, N, m):
        out = []
        for i in np.argsort(prof)[::-1]:
            if 0.12 * m < i < 0.88 * m and all(abs(i - j) > m * 0.12 for j in out):
                out.append(int(i))
            if len(out) >= N:
                break
        return sorted(out)
    n = [3, 5, 2][k - 1]
    xb = [0] + peaks(gx, n, P.W) + [P.W]
    yb = [0] + peaks(gy, n, P.H) + [P.H]
    picks = np.array([pal["sky"], pal["bld"], pal["moon"], pal["dark"], pal["ink"]], np.float32)
    i = np.zeros((P.H, P.W, 3), np.float32) + np.array(pal["bld"], np.float32)
    for a in range(len(xb) - 1):
        for b in range(len(yb) - 1):
            cell = P.orig[yb[b]:yb[b+1], xb[a]:xb[a+1]]
            if cell.size == 0:
                continue
            avg = cell.reshape(-1, 3).mean(0)
            i[yb[b]:yb[b+1], xb[a]:xb[a+1]] = picks[int(np.argmin(np.linalg.norm(picks - avg, axis=1)))]
    im = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    lw = max(3, P.W // 120)
    for x in xb: d.line([x, 0, x, P.H], fill=pal["ink"], width=lw)
    for y in yb: d.line([0, y, P.W, y], fill=pal["ink"], width=lw)
    return np.asarray(im).astype(np.float32)


def st_opart(P: Plate, pal, k):
    """光效应：条纹带宽由局部明暗决定（暗处带更宽）→ 既保留地形感，块均色也贴近原作"""
    i = P.orig.astype(np.float32) * 0.35 + 255.0 * 0.65
    L = cv2.GaussianBlur(CB(P.L), (0, 0), max(3.0, P.H * 0.012))
    yy = np.mgrid[0:P.H, 0:P.W][0].astype(np.float32)
    sp = max(8, P.H // [56, 38, 90][k - 1])
    phase = yy + (255.0 - L) * [0.55, 0.9, 0.35][k - 1]
    duty = 0.12 + 0.68 * (1.0 - L / 255.0)
    band = (phase % sp) < (sp * duty)
    ink = np.array(pal["ink"], np.float32)
    i[band] = i[band] * 0.25 + ink * 0.75
    ys = np.where((P.bld > 0.5).any(axis=1))[0]
    if len(ys):
        hy = int(ys.mean())
        i[hy:hy + max(2, P.H // 160)] = np.array(pal["moon"], np.float32)
    return np.clip(i, 0, 255)


def st_kandinsky(P: Plate, pal, k):
    """康定斯基：几何元素按画面明暗密度分布，颜色取该处照片色"""
    im = Image.fromarray(np.clip(solid(P.H, P.W, pal["bld"]), 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    small = cv2.resize(P.orig, (28, 28), interpolation=cv2.INTER_AREA).astype(np.float32)
    Ls = cv2.resize(P.L, (28, 28), interpolation=cv2.INTER_AREA) / 255.0
    rng = np.random.default_rng(61 + k)
    for gy in range(28):
        for gx in range(28):
            wgt = 1.0 - float(Ls[gy, gx])
            if wgt < 0.15:
                continue
            x = (gx + rng.random()) / 28 * P.W; y = (gy + rng.random()) / 28 * P.H
            col = tuple(int(v) for v in small[gy, gx])
            r = P.W * (0.006 + 0.028 * wgt)
            t = rng.random()
            if t < 0.45:
                d.ellipse([x - r, y - r, x + r, y + r], fill=col)
            elif t < 0.75:
                d.line([x - r, y, x + r, y], fill=col, width=max(2, int(r * 0.8)))
            else:
                d.arc([x - r * 2, y - r * 2, x + r * 2, y + r * 2], rng.uniform(0, 360), rng.uniform(0, 360), fill=col, width=max(2, int(r * 0.7)))
    return np.asarray(im).astype(np.float32)


def st_splatter(P: Plate, pal, k):
    """行动绘画：泼彩的密度与颜色都取自原作（暗处密、颜色取该处照片色）"""
    i = P.orig.astype(np.float32) * 0.45 + 255.0 * 0.55
    small = cv2.resize(P.orig, (36, 36), interpolation=cv2.INTER_AREA).astype(np.float32)
    Ls = cv2.resize(P.L, (36, 36), interpolation=cv2.INTER_AREA) / 255.0
    im = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    rng = np.random.default_rng(71 + k)
    for gy in range(36):
        for gx in range(36):
            wgt = 1.0 - float(Ls[gy, gx])
            for _ in range(int(wgt * [3.0, 4.6, 1.6][k - 1])):
                x = (gx + rng.random()) / 36 * P.W; y = (gy + rng.random()) / 36 * P.H
                r = P.W * rng.uniform(0.004, 0.022) * (0.5 + wgt)
                col = tuple(int(v) for v in small[gy, gx])
                d.ellipse([x - r, y - r, x + r, y + r], fill=col)
                if rng.random() < 0.18:
                    d.line([x, y, x + rng.uniform(-5, 5), y + P.H * rng.uniform(0.02, 0.09)], fill=col, width=max(1, int(r * 0.5)))
    return np.asarray(im).astype(np.float32)


def st_cubism(P: Plate, pal, k):
    """立体主义：画面切成多边形面片，每片整体错位"""
    n = [26, 40, 16][k - 1]
    rng = np.random.default_rng(81 + k)
    seeds = np.stack([rng.uniform(0, P.W, n), rng.uniform(0, P.H, n)], 1).astype(np.float32)
    offs = np.stack([rng.normal(0, P.W * 0.045, n), rng.normal(0, P.H * 0.045, n)], 1).astype(np.float32)
    yy, xx = np.mgrid[0:P.H, 0:P.W].astype(np.float32)
    d2 = (xx[..., None] - seeds[None, None, :, 0]) ** 2 + (yy[..., None] - seeds[None, None, :, 1]) ** 2
    who = np.argmin(d2, axis=2).astype(np.int32)
    mx = (xx + offs[who, 0]).astype(np.float32); my = (yy + offs[who, 1]).astype(np.float32)
    out = cv2.remap(P.orig.astype(np.float32), mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    edge = np.zeros((P.H, P.W), bool)                       # 必须是布尔数组，否则会变成花式索引
    edge[:-1] |= (np.abs(np.diff(who, axis=0)) > 0)
    edge[:, :-1] |= (np.abs(np.diff(who, axis=1)) > 0)
    ink = np.array(pal["ink"], np.float32)
    out[edge] = out[edge] * 0.3 + ink * 0.7
    t = np.clip((out @ LW) / 255.0, 0, 1)[..., None]
    return np.clip(out * 0.72 + (np.array(pal["dark"], np.float32) * (1 - t) + np.array(pal["bld"], np.float32) * t) * 0.28, 0, 255)


def st_colorfield(P: Plate, pal, k):
    """色域（硬边）：按横向均色分成色带，颜色取自调色板"""
    n = [7, 10, 5][k - 1]
    i = P.orig.astype(np.float32).copy()
    picks = [pal["sky"], pal["bld"], pal["moon"], pal["dark"], pal["ink"]]
    for b in range(n):
        y0, y1 = int(P.H * b / n), int(P.H * (b + 1) / n)
        if y1 <= y0: continue
        avg = P.orig[y0:y1].reshape(-1, 3).mean(0)
        j = int(np.argmin([np.linalg.norm(avg - np.array(pp, np.float32)) for pp in picks]))
        i[y0:y1] = np.array(picks[j], np.float32)
        i[y0:y0 + max(1, P.H // 400)] = np.array(pal["ink"], np.float32)
    return np.clip(i, 0, 255)



# ============================== 扩展风格（第三批） ==============================
def st_pointillism(P: Plate, pal, k):
    """点彩：整幅分辨率上按格落点，颜色直接采自该处像素（前后景用点的大小区分）"""
    n = [7, 5, 10][k - 1]
    rng = np.random.default_rng(101 + k)
    out = np.zeros((P.H, P.W, 3), np.float32) + np.array([250, 247, 240], np.float32)
    r = max(2, int(round(n * 0.52)))
    for gy in range(0, P.H, n):
        for gx in range(0, P.W, n):
            c = P.orig[min(P.H - 1, gy + n // 2), min(P.W - 1, gx + n // 2)]
            jx = int(gx + n * 0.5 + rng.uniform(-n * 0.16, n * 0.16))
            jy = int(gy + n * 0.5 + rng.uniform(-n * 0.16, n * 0.16))
            cv2.circle(out, (jx, jy), r, tuple(float(v) for v in c), -1, cv2.LINE_AA)
    return np.clip(out, 0, 255)


def st_newsprint(P: Plate, pal, k):
    """报纸粗网：大颗粒网点 + 暖灰新闻纸"""
    n = [9, 6, 13][k - 1]
    w2, h2 = max(24, P.W // n), max(24, P.H // n)
    lum = cv2.resize(P.L, (w2, h2), interpolation=cv2.INTER_AREA) / 255.0
    out = np.zeros((P.H, P.W, 3), np.float32) + np.array([231, 226, 214], np.float32)
    ink = (34.0, 32.0, 30.0)
    rmax = n * 0.62
    for gy in range(h2):
        for gx in range(w2):
            v = 1.0 - float(lum[gy, gx])
            if v <= 0.08:
                continue
            c0 = (int((gx + 0.5) * n), int((gy + 0.5) * n))
            cv2.circle(out, c0, max(1, int(round(rmax * math.sqrt(v)))), ink, -1, cv2.LINE_AA)
    rng = np.random.default_rng(111 + k)
    for _ in range(P.W * P.H // 900):
        x, y = int(rng.integers(0, P.W)), int(rng.integers(0, P.H))
        out[y, x] = out[y, x] * 0.94
    return np.clip(out, 0, 255)


def st_fauve(P: Plate, pal, k):
    """野兽派：保留原作明暗与色块结构，色相整体野兽化（高饱和非写实色）"""
    hsv = cv2.cvtColor(P.orig.astype(np.uint8), cv2.COLOR_RGB2HSV).astype(np.float32)
    h, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    shift = [70, 120, 35][k - 1]
    h2 = (h + shift + (val / 255.0) * 25.0) % 180
    s2 = np.clip(sat * [1.9, 2.4, 1.5][k - 1] + 40, 0, 255)
    step = [42, 56, 30][k - 1]
    v2 = np.clip(np.round(val / step) * step, 0, 255)
    out = cv2.cvtColor(np.stack([h2, s2, v2], -1).astype(np.uint8), cv2.COLOR_HSV2RGB).astype(np.float32)
    return np.clip(out * 0.92 + 255 * 0.08, 0, 255)


def st_risomis(P: Plate, pal, k):
    """错版套印：照片按 4 个色版量化，各版错位 2-8px 叠印"""
    inks = np.array([pal["bld"], pal["sky"], pal["moon"], pal["ink"]], np.float32)
    w2, h2 = max(24, P.W // 4), max(24, P.H // 4)
    small = cv2.resize(P.orig, (w2, h2), interpolation=cv2.INTER_AREA).astype(np.float32)
    dists = np.linalg.norm(small[..., None, :] - inks[None, None, :, :], axis=-1)
    which = np.argmin(dists, axis=-1)
    sh = [5, 8, 3][k - 1]
    out = np.zeros((P.H, P.W, 3), np.float32) + 255.0
    offs = [(0, 0), (sh, -sh // 2), (-sh // 2, sh), (sh // 2, sh // 2)]
    for idx in range(4):
        m = cv2.resize((which == idx).astype(np.float32), (P.W, P.H), interpolation=cv2.INTER_LINEAR)
        mx = np.float32([[1, 0, offs[idx][0]], [0, 1, offs[idx][1]]])
        m = cv2.warpAffine(m, mx, (P.W, P.H), borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        c = np.array(inks[idx], np.float32) / 255.0
        a = np.clip(m, 0, 1)[..., None] * 0.92
        out = out * (1 - a) + (out * c[None, None]) * a
    return np.clip(out, 0, 255)


def st_weave(P: Plate, pal, k):
    """织锦：横竖条交替穿插（棋盘格错开），整幅可辨、颜色对得上"""
    cw, ch = [18, 26, 12][k - 1], [12, 18, 8][k - 1]
    o = P.orig.astype(np.float32)
    i = o.copy()
    for by, y0 in enumerate(range(0, P.H, ch)):
        for bx, x0 in enumerate(range(0, P.W, cw)):
            y1, x1 = min(P.H, y0 + ch), min(P.W, x0 + cw)
            if (bx + by) % 2 == 0:
                i[y0:y1, x0:x1] = np.roll(o, int(cw * 0.5) * (1 if bx % 3 == 0 else -1), axis=1)[y0:y1, x0:x1]
            else:
                i[y0:y1, x0:x1] = np.roll(o, int(ch * 0.6) * (1 if by % 3 == 0 else -1), axis=0)[y0:y1, x0:x1]
    ink = np.array(pal["ink"], np.float32)
    for y0 in range(0, P.H, ch):
        i[y0:y0 + 1] = i[y0:y0 + 1] * 0.72 + ink * 0.28
    return np.clip(i, 0, 255)


def st_stamp(P: Plate, pal, k):
    """邮票拼版：整幅照片分格，格边打齿孔 + 角上盖销戳"""
    nx, ny = [3, 4, 2][k - 1], [2, 3, 2][k - 1]
    im = Image.fromarray(np.clip(P.orig.astype(np.float32) * 0.96, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    fr = (250, 248, 244)
    for a in range(1, nx):
        x = int(P.W * a / nx)
        for y in range(0, P.H, max(6, P.H // 90)):
            d.ellipse([x - 3, y, x + 3, y + 6], fill=fr)
    for b in range(1, ny):
        y = int(P.H * b / ny)
        for x in range(0, P.W, max(6, P.W // 90)):
            d.ellipse([x, y - 3, x + 6, y + 3], fill=fr)
    rng = np.random.default_rng(131 + k)
    for a in range(nx):
        for b in range(ny):
            cx = int((a + 0.5) * P.W / nx); cy = int((b + 0.5) * P.H / ny)
            rr = int(min(P.W / nx, P.H / ny) * 0.16)
            d.arc([cx - rr, cy - rr, cx + rr, cy + rr], float(rng.uniform(0, 120)), float(rng.uniform(200, 330)),
                  fill=(120, 96, 88), width=max(2, P.W // 500))
    return np.asarray(im).astype(np.float32)


def st_foil(P: Plate, pal, k):
    """烫金压印：单色金箔底 + 由照片梯度生成的浮雕明暗"""
    foil = np.array(pal["moon"], np.float32) * 0.55 + np.array([196, 158, 86], np.float32) * 0.45
    L = cv2.GaussianBlur(CB(P.L), (0, 0), max(2.0, P.W * 0.004))
    gx = cv2.Sobel(L, cv2.CV_32F, 1, 0, 3); gy = cv2.Sobel(L, cv2.CV_32F, 0, 1, 3)
    emb = np.clip((-gx - gy) * [0.35, 0.6, 0.2][k - 1], -70, 70)
    lum = (L / 255.0)[..., None]
    out = (np.zeros((P.H, P.W, 3), np.float32) + foil) * (0.70 + 0.52 * lum) + emb[..., None] * np.array([1.0, 0.92, 0.72], np.float32)
    rng = np.random.default_rng(141 + k)
    return np.clip(out + rng.normal(0, 4, (P.H, P.W, 1)).astype(np.float32), 0, 255)


def st_neon(P: Plate, pal, k):
    """霓虹：暗底 + 照片边缘发光（青/品红双色）"""
    L = cv2.GaussianBlur(CB(P.L), (0, 0), max(1.5, P.W * 0.002))
    gx = cv2.Sobel(L, cv2.CV_32F, 1, 0, 3); gy = cv2.Sobel(L, cv2.CV_32F, 0, 1, 3)
    g = np.sqrt(gx * gx + gy * gy)
    g = g / (float(np.percentile(g, 99)) + 1e-6)
    glow = cv2.GaussianBlur(np.clip(g, 0, 1), (0, 0), max(2.0, P.W * [0.006, 0.010, 0.004][k - 1]))
    lumf = (L / 255.0)[..., None]
    base = P.orig.astype(np.float32) * 0.30 * (0.35 + 0.95 * lumf)      # 保留原作明暗结构
    twe = lumf
    mix = np.array([80, 240, 230], np.float32)[None, None] * (1 - twe) + np.array([250, 80, 200], np.float32)[None, None] * twe
    out = base + mix * (glow[..., None] * 1.3) * (0.4 + 0.8 * lumf) + mix * (np.clip(g, 0, 1)[..., None] * 0.45) * lumf
    return np.clip(out, 0, 255)


def st_lightleak(P: Plate, pal, k):
    """胶片漏光：照片 + 边角暖色漏光 + 颗粒"""
    i = P.orig.astype(np.float32).copy()
    yy, xx = np.mgrid[0:P.H, 0:P.W].astype(np.float32)
    rng = np.random.default_rng(151 + k)
    for _ in range([2, 3, 1][k - 1]):
        cx, cy = float(rng.uniform(0, P.W)), float(rng.uniform(-0.1 * P.H, 1.1 * P.H))
        rr = float(rng.uniform(0.3, 0.75)) * P.W
        leak = np.clip(1 - np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / rr, 0, 1) ** 2
        col = np.array([250, 150, 70], np.float32) if rng.random() < 0.6 else np.array([255, 220, 120], np.float32)
        i = i * (1 - leak[..., None] * 0.55) + col[None, None] * (leak[..., None] * 0.55)
    return np.clip(i * 0.97 + rng.normal(0, 7, (P.H, P.W, 1)).astype(np.float32), 0, 255)


def st_dada(P: Plate, pal, k):
    """达达拼贴：照片裁块（旋转）+ 几何纸片 + 票据式条块"""
    i = P.orig.astype(np.float32) * 0.55 + 255 * 0.45
    rng = np.random.default_rng(161 + k)
    im = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    ys, xs = np.where(P.bld > 0.5)
    for _ in range([4, 6, 3][k - 1]):
        w = int(P.W * rng.uniform(0.22, 0.42)); h = int(P.H * rng.uniform(0.16, 0.32))
        cy = int(rng.choice(ys)) if len(ys) else P.H // 2
        cx = int(rng.choice(xs)) if len(xs) else P.W // 2
        y0 = int(np.clip(cy - h // 2, 0, P.H - h)); x0 = int(np.clip(cx - w // 2, 0, P.W - w))
        patch = Image.fromarray(P.orig[y0:y0 + h, x0:x0 + w].astype(np.uint8)).rotate(float(rng.uniform(-9, 9)), expand=True, resample=Image.BICUBIC)
        ty = int(np.clip(y0 + rng.normal(0, P.H * 0.03), 0, max(0, P.H - patch.height)))
        tx = int(np.clip(x0 + rng.normal(0, P.W * 0.03), 0, max(0, P.W - patch.width)))
        im.paste(patch, (tx, ty))
        d.rectangle([tx, ty, tx + patch.width - 1, ty + patch.height - 1], outline=(250, 248, 244), width=max(2, P.W // 400))
    for _ in range([3, 4, 2][k - 1]):
        w = int(P.W * rng.uniform(0.12, 0.3)); h = int(P.H * rng.uniform(0.03, 0.08))
        x0 = int(rng.uniform(0, max(1, P.W - w))); y0 = int(rng.uniform(0, max(1, P.H - h)))
        col = [pal["sky"], pal["moon"], pal["ink"]][int(rng.integers(0, 3))]
        d.rectangle([x0, y0, x0 + w, y0 + h], fill=tuple(int(v) for v in col))
    return np.asarray(im).astype(np.float32)


def st_rothko(P: Plate, pal, k):
    """竖直色光：按列均色分竖带，带边粗糙笔触（硬边，不模糊）"""
    n = [5, 7, 3][k - 1]
    picks = np.array([pal["sky"], pal["moon"], pal["dark"], pal["bld"], pal["ink"]], np.float32)
    out = P.orig.astype(np.float32).copy()
    rng = np.random.default_rng(171 + k)
    for b in range(n):
        x0, x1 = int(P.W * b / n), int(P.W * (b + 1) / n)
        if x1 <= x0:
            continue
        col = P.orig[:, x0:x1].reshape(-1, 3).mean(0)      # 用照片自身列均色 → 保留痕迹
        out[:, x0:x1] = col
        jag = rng.integers(0, max(2, P.H // 40), P.H)
        for y in range(P.H):
            j = int(jag[y])
            if j:
                out[y, max(0, x0 - j):x0 + 1] = col * 0.8 + 255 * 0.2
    return np.clip(out, 0, 255)


def st_minimal(P: Plate, pal, k):
    """极简几何：纸色 + 大圆（取最亮团块位置）+ 一条地平线"""
    Ls = cv2.resize(P.L, (20, 20), interpolation=cv2.INTER_AREA)
    gy, gx = np.unravel_index(int(np.argmax(Ls)), Ls.shape)
    ys, xs = np.where(P.bld > 0.5)
    hy = int(ys.mean()) if len(ys) else int(P.H * 0.6)
    top = np.median(P.orig[:max(4, hy // 2)].reshape(-1, 3), axis=0)
    wash = cv2.GaussianBlur(P.orig.astype(np.float32), (0, 0), max(3.0, P.W * 0.012))
    paper = solid(P.H, P.W, pal["bld"]) * 0.52 + wash * 0.48      # 极淡的原作底
    im = Image.fromarray(np.clip(paper, 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    cx, cy = (gx + 0.5) / 20 * P.W, (gy + 0.5) / 20 * P.H
    r = P.W * [0.30, 0.42, 0.20][k - 1]
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=tuple(int(v) for v in top))
    d.line([0, hy, P.W, hy], fill=tuple(int(v) for v in pal["ink"]), width=max(2, P.H // 200))
    return np.asarray(im).astype(np.float32)


def st_futurism(P: Plate, pal, k):
    """未来主义：画面沿对角剪切 + 速度线（保留原作结构）"""
    sh = [0.35, 0.55, 0.2][k - 1]
    M = np.float32([[1, sh * 0.5, -sh * P.H * 0.25], [sh * 0.35, 1, -sh * P.W * 0.1]])
    out = cv2.warpAffine(P.orig.astype(np.float32), M, (P.W, P.H), borderMode=cv2.BORDER_REFLECT)
    out = out * 0.82 + 255 * 0.18
    rng = np.random.default_rng(181 + k)
    ink = tuple(float(v) for v in pal["ink"])
    for _ in range([14, 22, 8][k - 1]):
        x0 = float(rng.uniform(0, P.W)); y0 = float(rng.uniform(0, P.H)); ln = float(rng.uniform(0.25, 0.8)) * P.W
        ang = float(rng.uniform(-0.5, 0.5)) + sh
        cv2.line(out, (int(x0), int(y0)), (int(x0 + ln * math.cos(ang)), int(y0 + ln * math.sin(ang))), ink,
                 max(2, int(P.W * 0.003)), cv2.LINE_AA)
    return np.clip(out, 0, 255)


def st_construct(P: Plate, pal, k):
    """构成主义：画面分成横带（取真实行均色）+ 斜线与圆"""
    n = [4, 6, 3][k - 1]
    out = P.orig.astype(np.float32).copy()
    for b in range(n):
        y0, y1 = int(P.H * b / n), int(P.H * (b + 1) / n)
        if y1 <= y0:
            continue
        out[y0:y1] = P.orig[y0:y1].reshape(-1, 3).mean(0)
    im = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    ys, xs = np.where(P.bld > 0.5)
    cx = int(xs.mean()) if len(xs) else P.W // 2
    cy = int(ys.mean()) if len(ys) else P.H // 2
    ink = tuple(int(v) for v in pal["ink"]); acc = tuple(int(v) for v in pal["moon"])
    d.polygon([(cx, cy - P.H * 0.30), (cx + P.W * 0.12, cy + P.H * 0.26), (cx - P.W * 0.12, cy + P.H * 0.26)], fill=acc)
    d.line([0, P.H * 0.86, P.W, P.H * 0.14], fill=ink, width=max(3, P.H // 90))
    d.ellipse([cx - P.W * 0.06, cy - P.H * 0.04, cx + P.W * 0.06, cy + P.H * 0.08], outline=ink, width=max(3, P.H // 120))
    return np.asarray(im).astype(np.float32)

def st_polar(P: Plate, pal, k):
    """极坐标（涡镜）：只在中央圆形区域做极坐标重映射，其余保留原作布局"""
    H, W = P.H, P.W
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    cx, cy = W // 2, H // 2
    maxr = math.sqrt(cx * cx + cy * cy)
    ang = np.arctan2(yy - cy, xx - cx); rad = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    m_x = (rad / maxr * W).astype(np.float32); m_y = ((ang / math.pi * 0.5 + 0.5) * H).astype(np.float32)
    war = cv2.remap(P.orig.astype(np.float32), m_x, m_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    rr = min(W, H) * [0.40, 0.30, 0.48][k - 1]
    m = np.clip((rr - rad) / max(2.0, W * 0.012) + 0.5, 0, 1)[..., None]
    out = P.orig.astype(np.float32) * (1 - m) + war * m
    im = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=tuple(int(v) for v in pal["moon"]), width=max(2, int(W * 0.004)))
    return np.asarray(im).astype(np.float32)


def st_motion(P: Plate, pal, k):
    ln = [40, 80, 20][k - 1]
    acc = np.zeros((P.H, P.W, 3), np.float32)
    for t in range(0, ln, 4):
        M = np.float32([[1, 0, t * 0.6], [0, 1, t]])
        acc += cv2.warpAffine(P.orig.astype(np.float32), M, (P.W, P.H), borderMode=cv2.BORDER_REPLICATE)
    return np.clip(acc / (ln / 4.0) * 1.05, 0, 255)


def st_duotone(P: Plate, pal, k):
    t = np.clip((P.orig.astype(np.float32) @ LW) / 255.0, 0, 1)[..., None]
    i = np.array(pal["sky"], np.float32) * (1 - t) + np.array(pal["moon"], np.float32) * t
    return over(i, pal["moon"], P.moon * 0.9)


def st_papercut(P: Plate, pal, k):
    i = solid(P.H, P.W, pal["sky"])
    for dep, m in [(0, P.moon), (1, P.bld)]:
        sh = np.roll(np.roll(m, 14 + 8 * dep, 0), 14 + 8 * dep, 1); sh = cv2.GaussianBlur(sh, (0, 0), 10 + 6 * dep)
        i = i * (1 - sh[..., None] * 0.5) + np.array(pal["ink"], np.float32) * 0.5 * sh[..., None]
        i = over(i, pal["dark"] if dep == 0 else pal["bld"], m)
    i = over(i, pal["moon"], P.moon)
    return i


def st_cyanotype(P: Plate, pal, k):
    t = np.clip((P.orig.astype(np.float32) @ LW) / 255.0, 0, 1)[..., None]
    # 普鲁士蓝白晒
    i = np.array((240, 246, 252), np.float32) * t + np.array((14, 44, 88), np.float32) * (1 - t)
    gx = cv2.Sobel(CB(P.Lsm), cv2.CV_32F, 1, 0, 3); gy = cv2.Sobel(CB(P.Lsm), cv2.CV_32F, 0, 1, 3)
    ln = cv2.GaussianBlur((np.sqrt(gx * gx + gy * gy) > 18).astype(np.float32), (0, 0), 0.7)[..., None]
    i = i * (1 - ln * 0.85) + np.array((236, 244, 252), np.float32) * ln * 0.85
    return np.clip(i, 0, 255)


def st_shards(P: Plate, pal, k):
    i = solid(P.H, P.W, pal["sky"]); rng = np.random.default_rng(k + 3)
    base = P.orig.astype(np.float32)
    for _ in range(90):
        cx, cy = rng.integers(0, P.W), rng.integers(0, P.H)
        s = rng.integers(P.W // 14, P.W // 5)
        pts = np.array([[cx, cy], [cx + s, cy + rng.integers(-s // 2, s // 2)],
                        [cx + rng.integers(-s // 2, s // 2), cy + s]], np.int32)
        mask = np.zeros((P.H, P.W), np.uint8); cv2.fillPoly(mask, [pts], 255)
        mm = cv2.GaussianBlur(mask.astype(np.float32) / 255.0, (0, 0), 0.8)
        dx, dy = int(rng.integers(-60, 60)), int(rng.integers(-60, 60))
        shifted = np.roll(np.roll(base, dy, 0), dx, 1)
        i = i * (1 - mm[..., None]) + shifted * mm[..., None]
    return i


def st_scanlines(P: Plate, pal, k):
    """丝网横线印：照片压成 墨/中间色/纸 三档，再压横线纹理；主体反白保持可读"""
    L = cv2.GaussianBlur(CB(P.L), (0, 0), 2.0)
    lo, hi = float(np.percentile(L, 30)), float(np.percentile(L, 68))
    ink = np.array(pal["ink"], np.float32); mid = np.array(pal["bld"], np.float32)
    pap = np.array(pal["sky"], np.float32); moon = np.array(pal["moon"], np.float32)
    i = np.where(L[..., None] < lo, ink, np.where(L[..., None] < hi, mid, pap)).astype(np.float32)
    i = over(i, moon, P.bld)
    hgt = [11, 7, 18][k - 1]
    bg = (1 - np.clip(P.bld, 0, 1))[..., None]
    ln = max(1, hgt // 5)
    for y in range(0, P.H, hgt):                           # 纸色横线只在背景上走
        seg = i[y:y + ln]
        i[y:y + ln] = seg * (1 - 0.5 * bg[y:y + ln]) + pap * 0.5 * bg[y:y + ln]
    return i


def st_riso4(P: Plate, pal, k):
    base = solid(P.H, P.W, (247, 240, 226))
    def plate(c, m, dx):
        a = np.zeros((P.H, P.W, 3), np.float32)
        a = a * (1 - m[..., None]) + np.array(c, np.float32) * m[..., None]
        return np.roll(a, dx, axis=1) / 255.0
    L1 = plate(pal["sky"], P.sky, -12)
    L2 = plate(pal["bld"], P.bld, 4)                              # 主体整块，保持轮廓/负空间
    L3 = plate(pal["ink"], np.clip(P.key * (P.bld > 0.5), 0, 1), 14)   # 墨版 = 主体轮廓线
    L4 = plate(pal["moon"], P.moon, -6)
    i = L1
    for L in (L2, L3, L4):
        i = 1 - (1 - i) * (1 - L)
    return np.clip(i * 255, 0, 255)


def st_inkmin(P: Plate, pal, k):
    i = solid(P.H, P.W, pal["bld"])
    i = i * (1 - 0.85 * P.bld[..., None]) + np.array(pal["dark"], np.float32) * 0.85 * P.bld[..., None]
    i = i * (1 - P.open[..., None] * 0.9) + np.array(pal["ink"], np.float32) * P.open[..., None] * 0.9
    i = over(i, pal["moon"], P.moon)
    return i


def st_blocks(P: Plate, pal, k):
    """色块分割：每块取该格照片均色（轻度量化，保留原作色彩）"""
    n = [14, 20, 9][k - 1]
    small = cv2.resize(P.orig, (n, n), interpolation=cv2.INTER_AREA).astype(np.float32)
    q = np.round(small / 34.0) * 34.0
    big = cv2.resize(q, (P.W, P.H), interpolation=cv2.INTER_NEAREST)
    im = Image.fromarray(np.clip(big, 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    step = P.W / n
    for i2 in range(n):
        d.line([i2 * step, 0, i2 * step, P.H], fill=(245, 243, 238), width=max(2, int(step * 0.05)))
        d.line([0, i2 * step, P.W, i2 * step], fill=(245, 243, 238), width=max(2, int(step * 0.05)))
    return np.asarray(im).astype(np.float32)



STYLES = {
    "hardedge": st_hardedge, "negative": st_negative, "halftone": st_halftone, "mirror": st_mirror,
    "strips": st_strips, "engraving": st_engraving, "pattern": st_pattern, "deco": st_deco,
    "flat": st_flat, "collage": st_collage, "pixel": st_pixel, "polar": st_polar,
    "motion": st_motion, "duotone": st_duotone, "papercut": st_papercut, "cyanotype": st_cyanotype,
    "shards": st_shards, "scanlines": st_scanlines, "riso4": st_riso4, "inkmin": st_inkmin,
    "blocks": st_blocks, "lineart": st_lineart, "glitch": st_glitch, "cross": st_cross,
    "quilt": st_quilt, "pixelsort": st_pixelsort, "mosaic": st_mosaic, "dotmatrix": st_dotmatrix,
    "film": st_film, "bauhaus": st_bauhaus, "supremat": st_supremat, "destijl": st_destijl,
    "opart": st_opart, "kandinsky": st_kandinsky, "splatter": st_splatter, "cubism": st_cubism,
    "colorfield": st_colorfield,
    "pointillism": st_pointillism, "newsprint": st_newsprint, "fauve": st_fauve, "risomis": st_risomis,
    "weave": st_weave, "stamp": st_stamp, "foil": st_foil, "neon": st_neon,
    "lightleak": st_lightleak, "dada": st_dada, "rothko": st_rothko, "minimal": st_minimal,
    "futurism": st_futurism, "construct": st_construct,
}

NAMES = {
    "hardedge": "硬边剪影", "negative": "负片", "halftone": "网点", "mirror": "镜像",
    "strips": "拼接条带", "engraving": "线刻版画", "pattern": "重复图案", "deco": "放射装饰",
    "flat": "剪纸平涂", "collage": "照片拼贴", "pixel": "像素画", "polar": "极坐标",
    "motion": "拖影", "duotone": "双色渐变", "papercut": "剪纸层叠", "cyanotype": "蓝晒",
    "shards": "碎片棱镜", "scanlines": "扫描色带", "riso4": "四色套印", "inkmin": "极简墨线",
    "blocks": "色块分割", "lineart": "轮廓线稿", "glitch": "故障化", "cross": "十字绣",
    "quilt": "拼布", "pixelsort": "像素排序故障", "mosaic": "马赛克", "dotmatrix": "点阵印刷",
    "film": "胶片接触印相", "bauhaus": "包豪斯几何", "supremat": "至上主义", "destijl": "风格派",
    "opart": "光效应", "kandinsky": "康定斯基", "splatter": "行动绘画", "cubism": "立体主义",
    "colorfield": "色域",
    "pointillism": "点彩", "newsprint": "报纸粗网", "fauve": "野兽派", "risomis": "错版套印",
    "weave": "织锦", "stamp": "邮票拼版", "foil": "烫金压印", "neon": "霓虹描边",
    "lightleak": "胶片漏光", "dada": "达达拼贴", "rothko": "竖直色光", "minimal": "极简几何",
    "futurism": "未来主义", "construct": "构成主义",
}


def suggest(plate: Plate, seed_pal: str | None = None) -> list:
    """按画面特征挑八格（风格不重复，配色随明暗切换，保证冷暖节奏）"""
    P = plate.orig.astype(np.float32)
    dark = float(np.median(plate.L)) < 110
    sat = float((P.max(-1) - P.min(-1)).mean())
    sky_cov = float(plate.sky.mean()); bld_cov = float(plate.bld.mean())
    pal = seed_pal or ("B" if dark else "E")
    pal_alt = "A" if pal != "A" else "B"
    pool = []      # (score, key)
    def add(st, sco, p=None):
        pool.append((sco, f"{st}_{p or pal}"))
    # 通用（不依赖光源）
    add("pixel", 9); add("glitch", 8); add("strips", 8); add("collage", 8)   # 用户偏好：像素/拼接/故障
    add("cross", 8); add("mosaic", 8); add("dotmatrix", 7); add("pixelsort", 7)
    add("bauhaus", 7); add("opart", 7); add("cubism", 7); add("splatter", 6)
    add("destijl", 6); add("supremat", 6); add("kandinsky", 5); add("colorfield", 5)
    add("film", 6); add("quilt", 6)
    add("flat", 5); add("hardedge", 4)
    add("riso4", 3); add("blocks", 3); add("cyanotype", 5 if dark else 4)
    add("motion", 6); add("scanlines", 2); add("engraving", 3)
    add("duotone", 5 if sat > 20 else 4); add("shards", 3); add("collage", 4)
    add("inkmin", 2, "E" if dark else "B"); add("motion", 3); add("polar", 4)
    add("negative", 2, "B" if dark else "E")
    # 依赖光源的装置
    if plate.has_focal:
        add("deco", 7); add("halftone", 6); add("pattern", 5); add("papercut", 5)
    if sky_cov > 0.35:
        add("hardedge", 6); add("flat", 5)
    if bld_cov < 0.22:
        add("pattern", 6); add("mirror", 5); add("negative", 5)
    if bld_cov > 0.55:
        add("mirror", 6); add("engraving", 5)
    if not dark:
        add("negative", 6, "D")
    # 弱痕迹风格不进默认推荐（仍可 --order 手动调用）：lineart/inkmin/engraving 为刻意单色，
    # pattern/riso4/shards/supremat/bauhaus/kandinsky/neon/rothko 为"意译"级
    _WEAK = {"lineart", "inkmin", "engraving", "pattern", "riso4", "shards",
             "supremat", "bauhaus", "kandinsky", "neon", "rothko"}
    pool = [(sc, k) for sc, k in pool if k.rsplit("_", 1)[0] not in _WEAK]
    pool.sort(reverse=True)
    chosen, seen = [], set()
    for sco, key in pool:
        st = key.rsplit("_", 1)[0]
        if st in seen:
            continue
        seen.add(st); chosen.append(key)
        if len(chosen) == 8:
            break
    # 冷暖交替排序（保证观感节奏）
    order_pref = ["pixel", "glitch", "pixelsort", "mosaic", "cross", "dotmatrix", "bauhaus", "opart",
                  "cubism", "splatter", "destijl", "kandinsky", "colorfield", "supremat", "strips", "collage",
                  "film", "quilt", "motion", "flat", "hardedge", "polar",
                  "deco", "halftone", "duotone", "cyanotype", "collage", "blocks", "polar", "strips",
                  "riso4", "engraving", "scanlines", "negative", "motion"]
    chosen.sort(key=lambda k: order_pref.index(k.rsplit("_", 1)[0]) if k.rsplit("_", 1)[0] in order_pref else 99)
    if len(chosen) >= 8:
        chosen[3] = chosen[3].rsplit("_", 1)[0] + "_" + pal_alt     # 让第四格换配色，避免全同色系
    return chosen[:8]


# ---------------------------------------------------------------- 渲染 / 组装
def render(plate: Plate, style: str, pal_name: str, k: int) -> Image.Image:
    fn = STYLES[style]; pal = PALETTES[pal_name]
    img = np.clip(fn(plate, pal, k), 0, 255)
    return Image.fromarray(img.astype(np.uint8))


def piece_name(style, pal):
    return f"{style}_{pal}"


def make_grid(centre: Image.Image, tiles: list[Image.Image], gut=6, margin=48, bg=(243, 241, 235), tile_w=512) -> Image.Image:
    ar = tiles[0].height / tiles[0].width
    ts = (tile_w, int(round(tile_w * ar)))
    gw, gh = ts[0] * 3 + gut * 2, ts[1] * 3 + gut * 2
    g = Image.new("RGB", (gw, gh), bg)
    allt = tiles[:4] + [centre] + tiles[4:]
    for i, t in enumerate(allt):
        r, c = divmod(i, 3)
        g.paste(t.resize(ts, Image.LANCZOS), (c * (ts[0] + gut), r * (ts[1] + gut)))
    if margin:
        m = Image.new("RGB", (gw + 2 * margin, gh + 2 * margin), bg)
        m.paste(g, (margin, margin)); return m
    return g


def run(src: Path, out: Path, order=None, center="photo", size=1152, gut=6, margin=48, single=None, tile_w=512,
        mode="auto", poly=None, mask=None, aspect="3:4", suggest_mode=False, render_all=False):
    out.mkdir(parents=True, exist_ok=True)
    plate = Plate(src, work=size, mode=mode, poly=poly, mask=mask, aspect=aspect)
    print("plate:", json.dumps(plate.describe(), ensure_ascii=False))
    if suggest_mode:
        order = suggest(plate)
        print("suggest:", ",".join(order))
    if render_all:
        allp = out / "all"; allp.mkdir(exist_ok=True)
        for st in STYLES:
            for pl in ("A", "B"):
                render(plate, st, pl, 1).save(allp / f"{st}_{pl}.png")
        print("all ->", allp, len(list(allp.iterdir())), "files")
    Image.fromarray(plate.orig).save(out / "plate_original.png")
    if single:
        st, pl = single.split("_")[0], single.split("_")[-1]
        render(plate, st, pl, 1).save(out / f"{single}.png"); print("single ->", out / f"{single}.png"); return
    order = order or ["hardedge_A", "halftone_B", "negative_B", "engraving_A", "pattern_B", "collage_A", "polar_C", "duotone_C"]
    pieces = []
    for key in order:
        st, pl = key.rsplit("_", 1)
        if st not in STYLES or pl not in PALETTES:
            print("  ! 未知风格/配色:", key); continue
        im = render(plate, st, pl, 1 + (len(pieces) % 3))
        im.save(out / "pieces" / f"{key}.png") if (out / "pieces").exists() else None
        pieces.append(im)
    (out / "pieces").mkdir(exist_ok=True)
    for i, key in enumerate(order):
        st, pl = key.rsplit("_", 1)
        if st in STYLES:
            render(plate, st, pl, 1 + (i % 3)).save(out / "pieces" / f"{key}.png")
    centre = Image.fromarray({ "photo": plate.orig, "plate": plate.plate }[center if center in ("photo", "plate") else "photo"])
    grid = make_grid(centre, pieces, gut=gut, margin=margin, tile_w=tile_w)
    grid.save(out / "nine_grid.png"); grid.save(out / "nine_grid.jpg", quality=95, subsampling=0)
    print("grid ->", out / "nine_grid.png", grid.size)


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="九宫格艺术风格工作流")
    ap.add_argument("--src", help="输入照片")
    ap.add_argument("--out", default="out", help="输出目录")
    ap.add_argument("--order", help="八个 style_PAL 键, 逗号分隔 (顺序=九宫格顺时针, 中心除外)")
    ap.add_argument("--center", default="photo", choices=["photo", "plate"])
    ap.add_argument("--size", type=int, default=1152)
    ap.add_argument("--gut", type=int, default=6)
    ap.add_argument("--margin", type=int, default=48)
    ap.add_argument("--tile", type=int, default=512, help="九宫格单格宽度(px), 默认 512")
    ap.add_argument("--aspect", default="3:4", choices=["3:4", "4:3", "1:1"], help="九宫格画幅")
    ap.add_argument("--suggest", action="store_true", help="按画面自动推荐八格")
    ap.add_argument("--all", action="store_true", help="渲染全部风格×配色到 out/all")
    ap.add_argument("--mode", default="auto", choices=["auto", "poly", "mask"], help="主体分离模式")
    ap.add_argument("--poly", help='主体多边形, 原图像素坐标: "x1,y1;x2,y2;..."')
    ap.add_argument("--mask", help="主体蒙版图片路径(白=主体)")
    ap.add_argument("--single", help="只渲染一张, 如 polar_C")
    ap.add_argument("--list", action="store_true", help="列出风格与配色")
    a = ap.parse_args()
    if a.list or not a.src:
        print("风格 styles:")
        for k, v in STYLES.items():
            print(f"  {k:10s} {CN.get(k, '')}")
        print("配色 palettes:")
        for k, v in PALETTES.items():
            print(f"  {k}: sky{v['sky']} bld{v['bld']} ink{v['ink']} moon{v['moon']} dark{v['dark']}")
        return
    run(Path(a.src), Path(a.out), order=a.order.split(",") if a.order else None,
        center=a.center, size=a.size, gut=a.gut, margin=a.margin, single=a.single, tile_w=a.tile,
        mode=a.mode, poly=a.poly, mask=a.mask, aspect=a.aspect,
        suggest_mode=a.suggest, render_all=a.all)


if __name__ == "__main__":
    main()
