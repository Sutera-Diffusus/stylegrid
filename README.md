<div align="center">

<img src="samples/hero.jpg" alt="stylegrid 示例：一张盐湖骑行照 → 九宫格" width="760">

# stylegrid · 风格网格

**一张照片进，一套作品出。**

**51 种艺术风格九宫格 · 16 个混血配方 · 竖/方/横三段海报 · 全程纯图片 · 自动质检。**

<img alt="license" src="https://img.shields.io/badge/license-MIT-blue">
<img alt="python" src="https://img.shields.io/badge/python-3.10%2B-3776ab">
<img alt="styles" src="https://img.shields.io/badge/styles-51-ff6b6b">
<img alt="mixes" src="https://img.shields.io/badge/mixes-16-845ef7">
<img alt="e2e" src="https://img.shields.io/badge/闭环测试-4%2F4%20PASS-brightgreen">
<img alt="regression" src="https://img.shields.io/badge/风格回归-10%2F10%20PASS-brightgreen">

[**一分钟上手**](#一分钟上手) · [**看看它长什么样**](#看看它长什么样) · [**它有什么**](#它有什么) · [**闭环与质检**](#闭环与质检) · [**常见问题**](#常见问题) · [English](README.en.md)

</div>

---

## 为什么是 stylegrid

大部分"照片转风格"工具要么给你一堆滤镜预览，要么把主体切得面目全非。stylegrid 的三条硬规则：

| 原则 | 具体做法 |
|---|---|
| **不糊** | 主体边界靠"边缘测算（边界是否落在真实强边缘上）+ 物体级分割"决定；**负空间必须保留**——轮圈内、车架三角要保持镂空，剪影才认得出是辆车。地面投影与车身同亮度时，用"纹理 + 形状"判据把它剔掉 |
| **有据** | 每件成品都算 **原作痕迹分**（`0.65×亮度布局相关 + 0.35×色彩接近度`）。分低的风格**不进默认推荐**，只在 `--order` 手动点名时可用；每份报告里逐件列出得分 |
| **闭环** | 工作流端到端测试（产物完整性 → 痕迹分达标率 → 非退化）＋ 风格库回归测试。改风格库必须过测试才敢交付 |

而且它是**真的在看你这张照片**：自动分析明暗、饱和度、主体形态、有没有真实光源，再决定挑哪八格、用哪套配色——**夜景会自动切到深色系，白天自动换成暖色系**。

---

## 目录

- [一分钟上手](#一分钟上手)
- [看看它长什么样](#看看它长什么样)
- [它有什么](#它有什么)
- [闭环与质检](#闭环与质检)
- [预设](#预设)
- [命令速查](#命令速查)
- [常见问题](#常见问题)
- [数据与隐私](#数据与隐私)
- [项目结构](#项目结构)
- [开发与测试](#开发与测试)
- [路线图](#路线图)
- [致谢](#致谢)
- [License](#license)

---

## 一分钟上手

```bash
git clone https://github.com/Sutera-Diffusus/stylegrid
cd stylegrid
pip install -r requirements.txt

python workflow.py --src 你的照片.jpg --out out/auto      # 全自动，先跑这个
```

打开 `out/auto/`：

```
01_九宫格.png      八格 + 原图对照
02_混血格.png      风格混血 3×3
03_海报_V.png      纯图片海报（默认零文字）
报告.md            画面分析 + 逐件痕迹分 + 文件清单
```

想要别的手感，三种粒度随你：

```bash
python workflow.py --src 照片.jpg --out out/x --profile 印刷          # 用预设
python workflow.py --src 照片.jpg --out out/x \
    --styles pixel_E,glitch_E,cross_E --mixes riso_pixel,ink_point \
    --posters V,S,H --aspect 1:1                                     # 全自定义
python workflow.py --list                                            # 列 51 风格 / 16 配方 / 6 预设
```

---

## 看看它长什么样

**盐湖骑行**（白天 · 无光源 · 自动选中像素/故障/拼贴家族）

| 九宫格 | 混血格 |
|---|---|
| <img src="samples/preview_bike_grid.jpg" width="420"> | <img src="samples/preview_bike_mix.jpg" width="420"> |

**月下高塔**（夜景 · 检测到月亮 · 自动切成深色系配色）

| 九宫格 | 混血格 |
|---|---|
| <img src="samples/preview_moon_grid.jpg" width="420"> | <img src="samples/preview_moon_mix.jpg" width="420"> |

**海报版式**（纯图片，无文字；需要题签时加 `--title`）

| 竖版 V | 方版 S | 横版 H |
|---|---|---|
| <img src="samples/preview_bike_poster_v.jpg" width="240"> | <img src="samples/preview_bike_poster_s.jpg" width="240"> | <img src="samples/preview_bike_poster_h.jpg" width="240"> |

---

## 它有什么

### 51 种艺术风格，分四个家族

| 家族 | 风格 |
|---|---|
| **照片保真**（保留照片质感） | `pixel` 像素画 · `dotmatrix` 点阵印刷 · `mosaic` 马赛克 · `cross` 十字绣 · `quilt` 拼布 · `strips` 拼接条带 · `collage` 照片拼贴 · `film` 胶片接触印相 · `glitch` 故障化 · `pixelsort` 像素排序故障 · `motion` 拖影 · `polar` 极坐标涡镜 · `scanlines` 扫描色带 · `halftone` 网点 · `pointillism` 点彩 · `newsprint` 报纸粗网 · `weave` 织锦 · `stamp` 邮票拼版 · `lightleak` 胶片漏光 · `fauve` 野兽派 · `risomis` 错版套印 · `futurism` 未来主义 |
| **图形剪影** | `flat` 剪纸平涂 · `hardedge` 硬边剪影 · `papercut` 剪纸层叠 · `inkmin` 极简墨线 · `lineart` 轮廓线稿 · `blocks` 色块分割 · `shards` 碎片棱镜 · `pattern` 重复图案 · `deco` 放射装饰 |
| **印刷色彩** | `riso4` 四色套印 · `engraving` 线刻版画 · `cyanotype` 蓝晒 · `duotone` 双色渐变 · `negative` 负片 · `mirror` 镜像 · `foil` 烫金压印 · `neon` 霓虹描边 · `rothko` 竖直色光 · `dada` 达达拼贴 |
| **抽象主义** | `bauhaus` 包豪斯几何 · `supremat` 至上主义 · `destijl` 风格派 · `opart` 光效应 · `kandinsky` 康定斯基 · `splatter` 行动绘画 · `cubism` 立体主义 · `colorfield` 色域 · `construct` 构成主义 · `minimal` 极简几何 |

每种的痕迹分与做法见 [`docs/风格清单.md`](docs/风格清单.md)。

### 16 个混血配方

引擎支持 8 种混合模式（`NRM/MUL/SCR/OVR/LGT/DRK/ADD/DIF`）× 三种作用域（`A` 整体 / `S` 仅主体 / `B` 仅背景），权重可调。

```bash
python mixposter.py --src 照片.jpg --out out/ --mix riso_pixel,ink_point
python mixposter.py --src 照片.jpg --out out/ --one "pixel_E*1+risomis_A:MUL@B"
```

其中 `split_*` 系列最出效果：**背景与主体各说各的语言**（例：背景野兽派狂色、主体保持套印写实）。

### 三段海报版式

`V` 竖版 / `S` 方版 / `H` 横版，**默认纯图片零文字**；传 `--title "标题" --sub "副题"` 才启用竖排题签柱与说明条。

### 手动兜底

自动分割不理想时，给一条主体多边形或一张蒙版即可：

```bash
python workflow.py --src 照片.jpg --out out/x --mode poly --poly "120,600;380,600;380,140;700,150;700,900;120,900"
python workflow.py --src 照片.jpg --out out/x --mode mask --mask fg.png
```

---

## 闭环与质检

**原作痕迹分** = `0.65 × |32×32 亮度布局相关系数| + 0.35 × 色彩接近度`，1.0 表示与原作完全一致。

- 51 种风格实测：**强痕迹（≥0.85）21 种 · 中（0.50–0.85）19 种 · 弱（<0.50）11 种**
  （弱项多为刻意单色或"意译"级抽象，仍可手动调用）
- 16 个混血配方实测：15 个 ≥0.70

**测试**：

```bash
python e2e_test.py 照片.jpg      # 闭环：工作流 → 产物完整性 → 痕迹分 → 非退化
python e2e_test.py               # 无参数也能跑（自动生成合成用例）
python selftest.py               # 风格库回归（10 类内容自动判定）
```

实测结果：闭环 **4/4 PASS**（痕迹分 min 0.81–0.86，达标率 100%）、风格回归 **10/10 PASS**。

---

## 预设

| 预设 | 风格族 | 配方 | 版式 |
|---|---|---|---|
| `照片保真` | 像素·故障·拼贴·条带·织锦·点彩·粗网·马赛克 | riso_pixel · glitch_weave · ink_point | V |
| `印刷` | 套印·粗网·十字绣·网点·线刻·扫描带·邮票·烫金 | print_cross · leak_riso · stamp_dada | S |
| `抽象` | 光效应·泼彩·立体·风格派·涡镜·色域·色块·网点 | mosaic_opart · dif_cross_opart | H |
| `剪影` | 平涂·硬边·剪纸·极简 ×2 配色 | split_flat_pixel · split_risomis_flat | V |
| `织品` | 十字绣·拼布·织锦·胶片·马赛克·点阵·像素·条带 | lgt_mosaic_glitch · split_weave_neon | S |
| `全都要` | auto | 8 个主力配方 | V,S,H |

改 `workflow.py` 里的 `PROFILES` 即增删；把自己常点的风格固定成一个预设，就能一条命令出全套。

---

## 命令速查

```bash
# 一站式
python workflow.py --src p.jpg --out out/ --profile 印刷 --title "标题"

# 九宫格（指定风格/画幅）
python gridkit.py --src p.jpg --out out/ --order pixel_E,glitch_E,cross_E --aspect 1:1

# 单张风格 / 全部风格
python gridkit.py --src p.jpg --out out/ --single risomis_A
python gridkit.py --src p.jpg --out out/ --all

# 混血
python mixposter.py --src p.jpg --out out/ --mix riso_pixel,ink_point

# 海报（纯图片 / 带题签）
python mixposter.py --src p.jpg --out out/ --poster V --style riso_pixel
python mixposter.py --src p.jpg --out out/ --poster V --style riso_pixel --title "标题" --sub "副题"
```

---

## 常见问题

**Q：主体分割不干净？**
先跑 `--list` 看风格，再用 `--mode poly --poly "x1,y1;…"` 给一条主体多边形；夜景/逆光/低对比照片最需要这一步。

**Q：某件成品痕迹分很低？**
报告里会标出来。换风格、换配色，或把它当"意译"作品看待——刻意单色（`lineart/inkmin/engraving`）与坐标重映射（`polar`）天然低分，不代表不可用。

**Q：能出文字版海报吗？**
可以，传 `--title/--sub` 即启用题签柱与说明条；默认是纯图片。

**Q：一张照片大概多久？**
900px 工作分辨率下，九宫格 + 混血格 + 三版海报 ≈ 10–40 秒（视风格而定，`cross/dotmatrix/pixelsort` 最慢）。

---

## 数据与隐私

**全部在本地跑，不联网、不上传、无遥测。** 你的照片只在本机读写；仓库里的示例图是可再生生成的合成题材。

---

## 项目结构

```
.
├── workflow.py        一站式入口：分析 → 选风格 → 九宫格/混血/海报 → 质检报告
├── gridkit.py         渲染主库：51 风格 + 5 配色 + 自适应主体分割 + 智能推荐
├── mixposter.py       混血引擎（混合模式 × 作用域）+ 版式系统
├── app/               本地 Web 工作室（server.py + 单页前端,零依赖)
├── promo/             宣传短片生成器（make_promo.py,PIL 逐帧渲染)
├── selftest.py        风格库回归测试
├── e2e_test.py        工作流闭环测试（自包含）
├── docs/风格清单.md    全风格说明 + 痕迹分总表
└── samples/           README 用示例成品
```

---

## 本地 Web 工作室

不喜欢命令行？仓库自带一个本地 Web 界面（苹果式单页：拖图 → 选风格卡 → 生成）:

```bash
python app/server.py              # 默认 http://127.0.0.1:8765/
python app/server.py --port 8791  # 端口被占用时换一个
```

- **风格预设是缩略图卡**,「自定义」展开 51 种风格多选池，直接改生成内容
- 「更多选项」里调色彩混合 / 视觉密度 / V·S·H 画幅 / 题签
- 最近作品自动读取 `out/web/` 历史产物，可拖拽横滑、点击回看、整包下载
- 与 CLI 同一引擎：`POST /api/generate` 走的就是 `workflow.run(...)`，产物落盘 `out/web/`

**宣传短片**:15 秒产品短片（PIL 逐帧渲染，无外部素材）:

```bash
python promo/make_promo.py        # 输出 promo/out/stylegrid_promo.mp4 + 分镜图 + JSON
```

---

## 开发与测试

```bash
python selftest.py      # 10 类内容回归，输出 _selftest/report.csv + 拼图
python e2e_test.py      # 闭环测试，输出 _selftest/e2e_report.json
```

改风格库的约定：新增风格 → 跑 `e2e_test.py` 拿痕迹分 → **≥0.50 才进默认推荐池**，否则只在 `--order` 可用。

---

## 路线图

- [ ] GitHub Actions：推代码自动跑闭环测试 + 徽章
- [ ] 配方自动搜索：给定 2–3 个风格，自动试混合模式与权重，挑痕迹分最高的组合
- [ ] 成套输出：一次出 V/S/H + 3×3 总览卡
- [ ] 更多设计语言：亚克力拼插、织锦多重经纬、字体版式（可选）

---

## 致谢

- 主体分割：OpenCV GrabCut + 自研"边缘测算 / 纹理形状剔除 / 负空间保护"三段式
- 调色板与风格体系：本项目原创
- 测试思路：以"痕迹分"作为可量化的交付门槛

---

## License

[MIT](LICENSE)
