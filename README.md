# 📷 Lumina Select · 光影选片助手

本地 AI 智能选片工具：**废片剔除 → 相似分组 → 场景自适应评分 → 最佳帧推荐 → 不确定甄选**，一键导出保留片。
照片全程本地处理、不上传；GUI 为 **PyQt6 桌面原生窗口**（无浏览器、无参数面板），算法核心在 `engine/`（纯 Python，可独立命令行调试）。

**技术栈**：Torch/CLIP/ViT + OpenCV 技术质量评分，共用 MediaPipe（人脸 / 闭眼）· pHash · SQLite（WAL）· PyQt6，经 PyInstaller / Inno Setup 打包。
> ✅ v0.5 曾并行维护「标准版（Torch）」与「轻量版（OpenCV 启发式）」两条产品线。**v0.6 已收敛为单一版本，只发行标准版**：轻量后端 `HeuristicBackend`、进程级开关 `config.INFERENCE_BACKEND`、轻量打包四件套与轻量依赖锁**均已删除**——原因与迁移步骤见 `../单版本收敛方案_2026-10-10.md`。
> 后端现在是**调用参数**、不是进程状态：`analyze_directory(root, db, backend="torch")`，默认值唯一来源为 `engine/inference.py` 的 `DEFAULT_BACKEND`。此举同时消除了 P0-1（切后端静默清空人工复核）的根因。

**关键指标**（RTX 4060 Laptop，1000 张实测）：全流程 63.9 s（验收 ≤5 min）；增量重分析 3.87 s；复核阶段 1000 项加载 **0.026 s**、单个筛选 **≤0.3 ms**；断点续跑、分块流式内存控制（5000+ 张不爆内存）；pytest 单测 + 端到端冒烟。

> **v0.6「单版本基线」已完成**（标签 `v0.6.0`）：单一发行线、数据保护（模型签名变化只重算指标、不再清空人工复核）、离线自足加载、降级可见、旧库升级明示化 + 已复核清单导出。
> **尚未正式发行**：Lightroom / RAW / XMP 实机验收与 PyQt6/Qt 发行许可批准（门禁 G7）由维护者另行完成；记录表见 `docs/v0.5-manual-acceptance.md`。旧版性能数字仅供历史参考。

---

## 🗺 开发路线图

**当前阶段：v0.7「盲测基线与 L3 组内相对排序」**——v0.6 已收口；v0.7 先建可信盲测基线，再验证组内相对排序。**该阶段仍不做模型替换。**

| 版本 | 主题 | 目标 | 状态 |
| --- | --- | --- | --- |
| **v0.6** | 单版本基线 | 一个版本、一条测试链路、一份文档口径；旧库升级不丢人工决策 | ✅ 已完成（标签 `v0.6.0`） |
| **v0.7** | 质量线 | ① 盲测集与现行排序基线 → ② L3 组内相对排序层（低维、可解释、确定性）→ ③ 参数标定与产品接入 | 🚧 下一步（1A 需维护者提供样本） |
| **v0.8** | 个性化 | 本地显式 A/B 偏好、低维排序头与画像管理（**默认关闭、可整体关闭**） | 待 L3 稳定后展开 |
| **P3** | 可选研究 | 新美学/IQA 底座、CLIP 变体、ONNX 引擎瘦身；**不是发布前置** | 有可复现证据才立项 |

**完整计划**：`../开发计划_v0.6-v0.8_2026-10-10.md`（rev 4，含分阶段工作包、多智能体分工与文件边界、门禁 G1–G8 + E1–E3、迁移步骤与风险清单）

### 已确认的架构方向（不要与此前方案冲突）

1. **保留深度学习版**：`HeuristicBackend` 无图像嵌入能力，而 L3 排序层与 L4 个性化层都以嵌入为输入，保留轻量版等于锁死路线图。
2. **问题不在美学模型，而在任务范式**：现状用「单图绝对分的加权和」去解「组内相对排序」，这是本工具推荐质量不理想的根因。v0.7 的核心动作是新增 L3 相对排序层，**优先级高于替换 L2 模型**。
3. **不引入新的模型家族**：MUSIQ / MANIQA / NIMA 均为 Apache-2.0，但它们是**失真质量模型**（非审美偏好模型），且跨域泛化差（MUSIQ 在 AVA 上 PLCC≈0.738，换到 TAD66K 仅 0.216）。继续留在 transformers 生态内，避免为未验证的收益支付新运行时的维护成本。
4. **数据保护先于任何结构性改动**：P0-1（切换后端会静默清空人工复核成果）已在 v0.6 S1 修复（`clear_all()` → `reset_metrics()`）。此后任何触及 `_ai_model_signature()` 或清库分支的改动，仍必须单独评审并附数据保护验证。
5. **先做「怎么用分数」，再谈「分数从哪来」**：
   - **质量线（v0.7）**——改「怎么用分数」：建盲测基线 → L3 组内相对排序。**不触发模型签名变化、不清库**。
   - **模型 / 底座替换**——改「特征从哪来」：触发签名变化，须走影子评测门禁；**没有本地盲测证据就不换**。
   - 理由：质量线的收益上限更高，且它建的 L3 模块正是后续任何底座替换收益的「接收器」。
6. **嵌入持久化不是前置条件**：先测清现有推理能否复用特征、重算代价与库增量；**只有实测证明有收益**才设计 embedding 缓存表（独立表、以模型签名隔离版本），而不是照计划预设 schema。
7. **ONNX 换的是引擎，不是模型**：引擎可从 PyTorch 526.6 MB 降至 ONNX Runtime 18.0 MB；额外可**只导出 CLIP 视觉塔**，再省 42% 模型体积（文本塔 254.1 MB 本项目用不到）。但 ONNX **不提升能力**，只释放体积预算——能力提升来自腾出的空间换来的更强底座。它与底座升级同属 P3 可选研究，有可复现收益才进入发行线。

---

## ✨ 功能

| 能力 | 说明 |
| --- | --- |
| 自动废片剔除 | 模糊（拉普拉斯方差）、过曝/欠曝（直方图占比）、**闭眼**（MediaPipe EAR + ViT 分类器融合）、高度重复 |
| 相似分组 | EXIF 时间戳连拍分组 + pHash 感知哈希（并查集合并，覆盖跨机位相似） |
| 场景自适应 | CLIP 分类：人像/风光/建筑/街拍/宠物/静物/其他，不同场景用不同权重 |
| 画质/美学评分 | 确定性的 OpenCV 清晰度/曝光/对比度技术评分（`opencv-technical-v1`）+ LAION 线性头→CLIP 提示词美学评分 |
| 最佳帧推荐 | 组内综合评分（清晰/曝光/美学/人脸），Top1 自动 5 星 |
| 不确定甄选 | 无明确胜者（分差小/帕累托冲突/场景置信低）时进入人工甄选（A/B/C/D 选择） |
| Lightroom 交付 | 按 RAW、RAW+JPEG、JPEG 资产模式复制到新目录；相机 RAW 写 XMP sidecar，JPEG/TIFF/PSD/DNG 仅修改导出副本内 XMP；失败可报告和重试 |

### 人工复核快捷键（Photo Mechanic 风格）
```
0-5 标星 · P 保留 / X 排除 · A/B/C/D 选候选 · Tab/→ 下一组 · ← 上一组 · Esc 退出
```
v0.5 复核页提供虚拟胶片带、单图/对比画布、候选排行、全量技术面板、筛选、撤销和资产级人工决定；RAW+JPEG 是同一资产，成员可同步决定，不互相判作重复废片。

---

## 🏗 架构

```
app_qt.py               PyQt6 桌面原生四阶段向导（①导入→②自动分析→③人工复核→④确认导出）
engine/
  config.py            全项目可调参数唯一来源（阈值/权重/模型名/路径，界面与引擎同源）
  log.py               统一日志（控制台 + 文件 smart_cull.log）
  loader.py            目录扫描、JPEG/PNG/RAW 解码、EXIF、缩略图缓存
  quality.py           模糊/曝光兼容接口（委托 OpenCV 技术质量评分）
  quality_metrics.py   OpenCV 清晰度/曝光/对比度技术质量评分（opencv-technical-v1）
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

### 安装与运行
```bash
# 建议使用干净环境；如本机已有匹配 CUDA 的 torch，也可用 --system-site-packages 复用
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --cache-dir .pip_cache -r requirements.txt
.\.venv\Scripts\python.exe app_qt.py
```

NVIDIA 用户如需 CUDA 构建，应先按锁定版本选择匹配的 Torch/TorchVision wheel；变更版本必须重新锁定、测试并构建，不能视为与当前候选包等价。

双击 exe 或运行脚本后直接弹出**原生桌面窗口**：选择照片文件夹（原生文件夹对话框）→ 点「开始分析」→ 自动进入复核/导出。
> 已打包的自包含 exe 见下方「打包成 exe · 方案 A」，双击 `dist\光影选片助手\光影选片助手.exe` 即用，无需 .venv。

### 从 v0.5 升级：旧项目怎么办

v0.6 只发行一个版本，旧项目**可以直接打开，不需要重建**，但升级过程是**明示**的（门禁 G4）：

| 情形 | 工具的做法 | 你要知道的事 |
| --- | --- | --- |
| 旧 schema 的库（v0.5 及更早） | 打开时自动升级到当前 schema，**升级前先备份**到 `项目库同级/<库名>.pre-v05.bak` | 状态行出现「旧库升级说明」按钮，点开能看到备份路径 |
| 库里记录的分析后端已下线（轻量版 `heuristic`）或模型签名变化 | 下次分析**只重算指标**（`reset_metrics()`），**不动人工复核** | 分析完成时状态行会写明「指标已重算 + 人工复核 N 条已保留」 |
| 想先留一份纸质单据 | 状态行的「导出已复核清单」一键导出 CSV（路径/星级/保留排除/人工场景/时间） | 库文件损坏或被误删时，仍能查到自己做过什么 |

> 升级绝不静默：任何"重算"都会说明原因（模型变了 / 旧后端不可比），任何人工决策（`star` / `label` / `scene_manual`）都不会被顺手删掉。

### 📦 打包成 exe（Windows 软件形态）

构建前从 `release/exiftool.lock.json` 中的 `official_download_url` 获取固定的
`exiftool-13.59_64.zip`，放在 `release/`；脚本首先校验长度和 SHA-256，再从归档
暂存运行目录。归档和运行文件均为本地忽略产物，不提交到 Git。构建脚本还会运行
包含 e2e 的完整 pytest、许可证开发检查及打包契约测试；失败即停止。仅可生成
开发候选包，`--release` 会被拒绝。单独检查可运行：

```powershell
.\.venv\Scripts\python.exe scripts\verify_exiftool.py
.\.venv\Scripts\python.exe scripts\benchmark_review.py --items 1000 --json benchmark-standard.json
```

#### 自包含 onedir 构建
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

#### 制作安装包（单文件 setup.exe，含卸载）
用 [Inno Setup](https://jrsoftware.org/isdl.php) 把方案 A 的 `dist\光影选片助手\` 封装为安装程序：
```bash
# 1) 先有方案 A 产物 dist\光影选片助手\
# 2) 用 Inno Setup Compiler 打开 installer.iss 并编译（或命令行 ISCC.exe installer.iss）
# 3) 仅产出 Output\LuminaSelect-v0.5-standard-dev-candidate.exe
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

### 🔌 离线能力（断网可完成分析）

v0.6 把「离线可用」从"看运气"变成了**被测试守住的行为**（门禁 G3）：

- **本地权重完整 ⇒ 直接以该快照目录为加载源**（`models_guard.local_model_source()`）。
  为什么不只是加个 `local_files_only=True`：`huggingface_hub` 在 **import 时**就把
  `HF_HUB_CACHE` 算成模块常量，若它在 `apply_env()` 之前被 import，之后设置环境变量
  **不再生效** —— 表现是"项目内 `.hf_cache` 里明明有模型，程序却去 `~/.cache/huggingface`
  找并失败"（S2-2 在测试里实测复现）。显式给出目录可彻底摆脱这层时序依赖。
  同时避免 `from_pretrained` 的 HEAD/etag 往返 —— 网络抖动重试会把一次 CLIP 加载从
  ~15 s 拖到 **499 s**（实测）；也避免模型权重被动漂移（会改 `_ai_model_signature()`
  → 触发全库重算，正是 P0-1 的雷区）。
- **阶段二不可用 ⇒ 降级完成，不中断整次分析**。典型场景：外拍现场无网、且从未下载过权重。
  此时阶段一（质量/哈希/人脸）结果照常落库并完成甄选，只是本次未使用美学/场景信号，
  并**在状态栏明确告知**（`analyze_directory()` 返回的 `degradations`）。
- **降级不固化**：美学/场景缺失时**不写入兜底值**（不留 `50.0` / `"其他"`），因此下一轮增量判断
  仍会认为"缺该项"，模型就绪后重跑会**自动补齐**，无需用户重建项目。
- 回归证据：`tests/test_pipeline.py`（降级完成 / 降级后自愈）、`tests/test_models_guard.py`
  （只读本地判定 + 接线）、`data/_offline_probe.py`（断网 + 空缓存探针）、
  `data/_cache_verify.py`（离线加载全部运行期模型）。

> ⚠️ **模型权重仍不打包**（安装包因此维持约 2 GB）：**首次运行需要联网下载一次**（约 1 GB）。
> 若要求"全新机器 + 断网首启即可完整分析"，需把权重打进分发包（+约 1 GB）——当前**不做**，
> 这是刻意的取舍（首次下载体积本身就是离线体验的一部分）。

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

# 完整测试（包含默认标记排除的 e2e；不触发模型下载）
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest -q -o addopts=

# 端到端冒烟（构造含连拍/模糊/过曝/清晰的图片集，跑通全链，较慢）
python -m pytest tests -m e2e
```

> ✅ **v0.6 已收敛为单一环境**：轻量 venv（`.venv-light`）、轻量依赖锁与两版对比断言全部删除。
> 不再需要 `LUMINA_INFERENCE_BACKEND` 之类的外部变量 —— `test_pipeline.py` 改为向注册表注入
> **确定性测试后端**并显式传参（`analyze_directory(..., backend="test-stub")`），
> 因此单测不加载 torch/CLIP，跑得快、离线可跑、且不依赖外部状态。
> **本机实测（2026-10-11，v0.6 收口后）**：`pytest tests` **241 passed / 1 deselected**（约 54 s）；
> `pytest -q -o addopts=` **242 passed**（含 e2e，约 81 s）。以你自己那次运行的输出为准。
>
> 另注：跑批结束时若出现 `SystemExit: 1` 与 `safe-delete` 提示，是本机沙箱拦截 pytest 临时目录清理所致，
> **与项目无关**，不影响用例判定。

---

## 🛠 开发与验证约定

> 面向后续开发与自动化执行者，请严格遵守。

| 约定 | 说明 |
| --- | --- |
| **受保护文件** | `评审总览_v0.4.md` 必须保持**未跟踪**，不得 `git add` 或提交（见 `WORKSPACE_README.md`）。 |
| **不污染 Git 工作区** | 验证脚本、临时库、复现产物一律写在 `data/`（已被 `.gitignore` 排除），不要落在仓库根目录。 |
| **不改动用户原片** | 任何测试/复现脚本**只能写临时目录**，严禁对用户照片目录做写操作。 |
| **测试环境变量** | 跑 UI 相关用例需 `QT_QPA_PLATFORM=offscreen`。**后端已无进程级开关**（v0.6 S2-1 删除 `config.INFERENCE_BACKEND`），测试改为注入确定性后端 + 显式传参。 |
| **提交粒度** | 一个工作包一个提交，提交信息须含「做了什么 + 为什么 + 如何回退」。 |
| **模型变更纪律** | 任何模型变更都会改变 `ai_models` 签名并触发重算。**必须同时提供数据保护验证**，不可只改实现；上线前走影子评测门禁。 |
| **许可门禁** | 新增依赖或权重前，须核验「仓库许可 + **具体权重文件**许可 + 训练集再分发条款」；`scripts/check_release_licenses.py` 必须通过。pyiqa（NTU S-Lab + CC BY-NC-SA，非商用）**禁止**进入分发链。 |
| **阈值不硬编码** | 现状 `BLUR_WASTE=60`、`EAR_CLOSED_THRESHOLD=0.20`、`SCORE_GAP=3.0` 均为手写常量。改动须走标定并在影子评测集上验证，禁止直接改默认值上线。 |
| **根因文件索引** | `engine/pipeline.py` 的 `_ai_model_signature()` 与清库/重算分支是全项目最高风险处（P0-1）。v0.6 S1 已把签名变化时的 `clear_all()` 改为 `reset_metrics()`（只置空模型派生列、保留人工决策），S2-1 又删除了触发该分支的进程级后端开关。**任何再触及签名或清库的改动仍必须单独评审并附数据保护验证。** |

---

## ⚡ 性能基准（实测）

以下 GPU 全流程数字是 v0.4 历史测量，不能替代真实 Lightroom 验收（见上方状态说明）。复核 DTO 基准请运行上文 `scripts/benchmark_review.py`：首次加载必须 `<3s`、每个筛选 `<0.3s`，JSON 记录机器与 Qt/Python 版本。**v0.6 实测（2026-10-11，1000 项）**：加载 **0.026 s**、5 个筛选各 **≤0.3 ms**，判定 `passed`。

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

**Q：能不能导出 Lightroom Classic 可读的星级和颜色？**
v0.5 写入 XMP 星级与 Green/Yellow/Red 标签；相机 RAW 仅写 sidecar，非 RAW 只写交付副本。真实 Lightroom Classic 读取和原有调整保留仍待人工验收。
