# 📷 Lumina Select · 光影选片助手

本地 AI 智能选片工具：**废片剔除 → 相似分组 → 场景自适应评分 → 最佳帧推荐 → 不确定甄选**，一键导出保留片。
照片全程本地处理、不上传；GUI 为 **PyQt6 桌面原生窗口**（无浏览器、无参数面板），算法核心在 `engine/`（纯 Python，可独立命令行调试）。

**双版本技术栈**：标准版使用 Torch/CLIP/ViT + OpenCV 技术质量评分；轻量版使用 OpenCV 启发式推理。两版共用 MediaPipe（人脸 / 闭眼）· pHash · SQLite（WAL）· PyQt6，并分别通过 PyInstaller / Inno Setup 独立打包。

**关键指标**（RTX 4060 Laptop，1000 张实测）：全流程 63.9 s（验收 ≤5 min）；增量重分析 3.87 s；断点续跑、分块流式内存控制（5000+ 张不爆内存）；pytest 单测 + 端到端冒烟。

> 当前版本 v0.4.0（可靠性与模型选型大修）。设计文档见上级目录 `PRD_智能选片工具/`。

---

## ✨ 功能

| 能力 | 说明 |
| --- | --- |
| 自动废片剔除 | 模糊（拉普拉斯方差）、过曝/欠曝（直方图占比）、**闭眼**（MediaPipe EAR + ViT 分类器融合）、高度重复 |
| 相似分组 | EXIF 时间戳连拍分组 + pHash 感知哈希（并查集合并，覆盖跨机位相似） |
| 场景自适应 | CLIP 分类：人像/风光/建筑/街拍/宠物/静物/其他，不同场景用不同权重 |
| 画质/美学评分 | 两版共享确定性的 OpenCV 清晰度/曝光/对比度技术评分；标准版使用 LAION 线性头→CLIP 提示词进行美学评分 |
| 最佳帧推荐 | 组内综合评分（清晰/曝光/美学/人脸），Top1 自动 5 星 |
| 不确定甄选 | 无明确胜者（分差小/帕累托冲突/场景置信低）时进入人工甄选（A/B/C/D 选择） |
| 一键导出 | CSV 清单 + 复制保留文件到导出目录 |

### 人工复核快捷键（Photo Mechanic 风格）
```
0-5 标星 · P 保留 / X 排除 · A/B/C/D 选候选 · Tab/→ 下一组 · ← 上一组 · Esc 退出
```
复核页还有「总览排行榜」：全局综合分排序、星级/废片/场景/推荐帧过滤、多字段排序、**场景手动修正入口**（写库持久，重分析不被覆盖）。

---

## 🏗 架构

```
app_qt.py               PyQt6 桌面原生四阶段向导（①导入→②自动分析→③人工复核→④确认导出）
engine/
  config.py            全项目可调参数唯一来源（阈值/权重/模型名/路径，界面与引擎同源）
  log.py               统一日志（控制台 + 文件 smart_cull.log）
  loader.py            目录扫描、JPEG/PNG/RAW 解码、EXIF、缩略图缓存
  quality.py           模糊/曝光兼容接口（委托 OpenCV 技术质量评分）
  quality_metrics.py   两版共享的 OpenCV 清晰度/曝光/对比度评分
  faces.py             MediaPipe 人脸 + 闭眼 EAR + ViT 分类器融合
  aesthetics.py        CLIP 美学评分（LAION-Aesthetics 线性头，GPU 优先）
  scene.py             CLIP 场景分类（人像/风光/其他；调试用，GUI 走 aesthetics 统一调用）
  similarity.py        pHash 相似、连拍分组、并查集聚类
  scorer.py            场景自适应评分、废片判定、最佳帧 + 不确定甄选
  models_guard.py      模型缓存自愈 / 离线优先 / 多级回退链（v0.4.0）
  store.py             SQLite 索引（WAL + 事务化 + 断点续跑辅助）
  pipeline.py          端到端编排（流式内存控制、线程池、断点续跑、增量、进度回调）
scripts/benchmark.py   性能分段基准脚本
tests/                 pytest 单元测试 + 端到端冒烟
```

### 数据流
```
扫描目录 → 逐张：OpenCV 技术质量/phash/人脸（流式+线程池，逐张落库）
       → CLIP 批量：美学+场景（逐批回写）
       → 相似聚类 → 组内场景自适应评分 → 废片/最佳帧/不确定甄选 → 全量入库
```

---

## 🚀 安装与运行

### 环境要求
- Windows / macOS / Linux，Python 3.10+（本项目在 Windows 11 + Python 3.12 验证）
- **GPU 可选**：有 NVIDIA GPU（CUDA）更快；无 GPU 自动降级 CPU（慢但可用）

### Torch 标准版：安装与运行
```bash
# 建议使用干净环境；如本机已有匹配 CUDA 的 torch，也可用 --system-site-packages 复用
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --cache-dir .pip_cache -r requirements.txt
.\.venv\Scripts\python.exe app_qt.py
```

NVIDIA 用户可先在 `.venv` 中安装与本机 CUDA 匹配的 Torch/TorchVision，再安装 `requirements.txt`；其中的版本范围不会替换已兼容的 GPU 构建。

### OpenCV 轻量版：安装与运行
```bash
python -m venv .venv-light
.\.venv-light\Scripts\python.exe -m pip install --cache-dir .pip_cache -r requirements-lightweight.txt
set LUMINA_INFERENCE_BACKEND=heuristic
.\.venv-light\Scripts\python.exe app_qt.py
```

双击 exe 或运行脚本后直接弹出**原生桌面窗口**：选择照片文件夹（原生文件夹对话框）→ 点「开始分析」→ 自动进入复核/导出。
> 已打包的自包含 exe 见下方「打包成 exe · 方案 A」，双击 `dist\光影选片助手\光影选片助手.exe` 即用，无需 .venv。

### 📦 打包成 exe（Windows 软件形态）

#### Torch 标准版：自包含 onedir 构建
把全部依赖（torch / transformers / PyQt6 / mediapipe / 等）一并打进一个文件夹，双击 `光影选片助手.exe` 即可运行，**不要求源码或 .venv 在场**：
```bash
# 一键打包自包含 onedir（首次约 3~8 分钟，体积较大）
build_dist.bat
# 可选：构建后额外生成 zip 压缩包
set ZIP=1 & build_dist.bat
```
- 产物：`dist\光影选片助手\` 文件夹（含 `光影选片助手.exe` + 全部依赖）。**整个文件夹拷贝到任意 Windows 机器双击即用**，无需 Python、无需 `.venv`。
- 模型权重（CLIP / 闭眼 ViT / MediaPipe）**不打包**，首次运行经 HF 镜像自动下载到 exe 目录下的 `.hf_cache` / `.torch_cache`（由 `dist_runtime_hook.py` 重定向，不落 C 盘）。
- 对应规格：`光影选片助手_dist.spec`（入口直接是 `app_qt.py`，显式保留 CLIP、ViT 与 FaceMesh 所需模块，不递归打包测试和无关模型族，且不包含 pyiqa）。
- **2026-09-07 本机实测**：干净 CPU Torch 环境构建约 5 分钟，onedir 产物 910.0 MiB / 6608 个文件；Inno Setup 安装包 228.4 MiB。

#### Torch 标准版：制作安装包（单文件 setup.exe，含卸载）
用 [Inno Setup](https://jrsoftware.org/isdl.php) 把方案 A 的 `dist\光影选片助手\` 封装为安装程序：
```bash
# 1) 先有方案 A 产物 dist\光影选片助手\
# 2) 用 Inno Setup Compiler 打开 installer.iss 并编译（或命令行 ISCC.exe installer.iss）
# 3) 产出 Output\光影选片助手_setup.exe
```
- 安装后提供**桌面快捷方式 + 开始菜单项 + 标准卸载**；卸载时默认清理 `.hf_cache`/`.torch_cache` 模型缓存（见 `installer.iss`）。
- 提示：模型会下载进安装目录，建议安装到有写入权限的位置；当前安装器使用用户级安装权限。

### GPU 与降级说明
- 有 CUDA GPU：CLIP / ViT 自动用 GPU，速度最快；OpenCV 技术质量评分保持 CPU 本地执行；
- 无 GPU / 驱动异常：自动回退 CPU，功能不变、仅更慢；
- 本次本机构建的标准安装包使用 `torch 2.14.0+cpu`；如需发布 CUDA 版，请先按 PyTorch 官方方式在干净 `.venv` 中安装匹配驱动的 CUDA Torch，再执行 `build_dist.bat`；
- `engine/aesthetics.py` 未找到 LAION 美学头时自动降级为「CLIP 提示词打分」；
- `rawpy` 未安装时自动跳过 RAW 扩展名，不影响 JPEG/PNG 全流程；
- 闭眼分类器（dima806）加载失败时自动退化为「仅 EAR」判定。

### 🪶 轻量版（无 torch，推荐普通用户分发）

标准版内嵌 torch / CLIP / ViT，体积较大。**轻量版**把这些深度学习推理
整体替换为纯 OpenCV 启发式（`engine/inference.py` 的 `HeuristicBackend`），画质/美学/场景
用图像特征估算，无需下载任何模型权重、完全离线：

```bash
# 首次构建前创建独立环境（不得复用含 Torch 的 .venv）
python -m venv .venv-light
.\.venv-light\Scripts\python.exe -m pip install --cache-dir .pip_cache -r requirements-lightweight.txt

# 一键打包轻量版 onedir
build_dist_lightweight.bat
# 可选：构建后额外生成 zip 压缩包
set ZIP=1 & build_dist_lightweight.bat
```
- 产物：`dist_light\光影选片助手\` 文件夹（含 `光影选片助手.exe` + 依赖）。整体拷贝到任意
  Windows 机器双击即用，**无任何联网/模型下载要求**，秒级启动。
- 能力对比：模糊 / 曝光 / 重复去重 / 人脸·闭眼（MediaPipe）**与标准版一致**（这些本就不依赖 torch）；
  画质分、美学分、场景分类改为 OpenCV 启发式，**精度低于深度学习模型**，由「人工复核」环节兜底。
- 切换开关：`engine/config.py` 的 `INFERENCE_BACKEND`（或环境变量 `LUMINA_INFERENCE_BACKEND`），
  可选 `torch`（标准，精度高）/ `heuristic`（轻量，离线）。轻量打包由
  `dist_runtime_hook_light.py` 强制写入 `heuristic`，开发态默认仍为 `torch`。
- 对应规格：`光影选片助手_dist_lightweight.spec`（排除 torch/transformers/pyiqa，保留 PyQt6/MediaPipe/opencv）。
- 轻量安装包：用 Inno Setup 编译 `installer_lightweight.iss`，输出到 `Output_light\光影选片助手轻量版_setup.exe`；它与标准版 `installer.iss`、`Output\` 完全独立，可并存安装。
- **2026-09-07 本机实测**：PyInstaller 6.22.2 最终构建约 97 秒，产物 411.1 MiB / 356 个文件；Inno Setup 安装包 103.4 MiB；
  不含 torch、transformers、pyiqa、tensorflow、jax、pytest 或 Sphinx。体积主要来自
  OpenCV-Contrib、MediaPipe FaceMesh、PyQt6 与 ImageHash 所需 SciPy。
- 真实 MediaPipe 0.10.21 FaceMesh 已在独立 `.venv-light` 中文路径环境中验证；标准版与轻量版使用
  各自的 ASCII junction，不会跨虚拟环境加载依赖。

### 模型缓存（不落 C 盘）
首次运行从 HuggingFace Hub 自动下载 CLIP / 闭眼 ViT / MediaPipe 权重，缓存于项目内
`.hf_cache` / `.torch_cache`（打包态由 `dist_runtime_hook.py` 重定向到 exe 旁；开发态默认落用户缓存目录）。离线可复用已缓存权重。

---

## 🧪 测试数据

```bash
# 演示集（28 张，含人像/风光/闭眼/相似组）
python make_demo_data.py

# 合成测试集（44 张：模糊/过曝/欠曝/人像/风光/连拍）
python make_test_data.py

# 性能集（1000 张：连拍/相似/废片，带 EXIF 时间戳）
python make_perf_data.py 1000
```

## ✅ 自动化测试

```bash
# 常规测试（含 quality/similarity/faces/scorer/store/pipeline/发行契约）
python -m pytest tests

# 端到端冒烟（构造含连拍/模糊/过曝/清晰的图片集，跑通全链，较慢）
python -m pytest tests -m e2e
```

---

## ⚡ 性能基准（实测）

**测试机**：Windows 11 · Python 3.12.12 · **NVIDIA GeForce RTX 4060 Laptop GPU**（CUDA）
**数据集**：`data/perf` 1000 张（1920×1080 级合成 JPEG/PNG，含连拍/废片/相似）

### 首轮完整分析（含模型加载 + 缩略图 + 推理）

| 阶段 | 耗时 | 占比 |
| --- | --- | --- |
| 扫描 | 4 ms | 0.0% |
| 读取元数据 | 8.24 s | 12.9% |
| 质量与哈希（画质+人脸+质量） | 25.21 s | 39.4% |
| 美学与场景（CLIP，GPU 批量） | 30.44 s | 47.6% |
| 相似聚类（pHash 复用） | 151 ms | 0.2% |
| 评分与甄选 | 30 ms | 0.0% |
| **合计** | **63.9 s** | — |

- **验收对照**：1000 张 GPU ≤5 min → **61.0 s 通过**；CPU ≤15 min 未测（无 CPU 机），理论 ~4-6 min（CPU 推理为 GPU 4-8 倍）。
- **增量分析**（mtime 未变，重复导入同目录）：**3.87 s**（只做聚类+评分，不重算任何图片）。
- **缩略图缓存**：首次 200 张 6.21 s；二次访问（缓存命中）0.12 s → 5000 张目录启动远低于 20 s。

### 断点续跑
阶段一/阶段二结果**逐张/逐批实时落库**（SQLite WAL 单事务）。分析中途断电/异常重启后，
只补算缺失部分（不从头再来）。实测：人为清掉 1 张的阶段一字段后重跑，`new_analyzed=1`。

### 手动抽样检查（人工一致性）
建议按「模糊 / 曝光 / 闭眼 / 相似组」每类抽 5-10 张人工复核，记录与引擎判定的一致率。
本项目 demo 集 28 张人工抽样：模糊、过曝、闭眼判定与预期一致；相似组边界场景
（时间戳缺失、极低对比度）建议以人工为准。

---

## 🔒 许可

- 项目代码：**Apache-2.0**（见 `LICENSE`）。
- pyiqa 已从正式运行依赖、标准 PyInstaller spec 和发行内容中移除。
- PyQt6/Qt 正式二进制发行必须有真实、可审计的许可批准依据；没有批准时只能构建开发候选包。
- 第三方包清单由 `scripts/check_release_licenses.py` 从精确锁和安装元数据生成；模型权重逐项说明见 `NOTICE.md`。

## 📄 更新日志
见 `CHANGELOG.md`。

---

## ❓ FAQ

**Q：分析很慢？**
先确认窗口底部状态栏显示「✅ GPU 推理」。无 GPU 时 CPU 全流程约 4-6 分钟/千张属正常。

**Q：为什么有的相似组进了「待甄选」？**
组内 Top1-Top2 综合分差小于 3、或维度互有胜负、或场景置信度低时，算法不替你拍板，
进入人工甄选（A/B/C/D 选一张）。

**Q：场景识别错了怎么办？**
「总览排行榜」里每张照片有场景下拉框，改成正确场景即可；该修正写入数据库，
重新分析不会被自动识别覆盖。

**Q：会把我照片传到网上吗？**
不会。所有处理在本机完成，仅在模型权重缺失时联网下载一次模型。

**Q：RAW 支持吗？**
扩展名识别已内置；安装 `rawpy` 后自动启用。未安装时仅处理 JPEG/PNG，不影响主流程。

**Q：能不能导出 Lightroom/Photo Mechanic 可读的星级？**
当前 MVP 提供 CSV 清单 + 复制原文件。XMP 星级写回（需 pyexiv2）列为后续路线。
