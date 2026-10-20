# Lumina Select · 光影选片助手

本地 AI 智能选片桌面工具：扫描照片、识别技术废片和相似组、给出组内推荐，供摄影师复核后导出。照片与项目库留在本机；Windows 桌面界面使用 PySide6，分析核心为 Python、OpenCV、Torch/CLIP/ViT，项目索引使用 SQLite。

## 当前状态

- 当前工作是**离线开发候选版**，不是正式发行版。支持 JPEG、PNG；安装 `rawpy` 后支持相机 RAW。CLIP 与闭眼 ViT 按 [模型锁定清单](release/models.lock.json) 固定；本地构建的候选 onedir 包从包内快照加载模型，不静默联网。模型权重和候选包不在 GitHub 仓库中。
- 自动场景对外统一为**人像、风景、街拍、其他**；低置信度保留候选类别并标记待确认。自动场景权重在独立人工样本通过验收前保持中性。最长边小于 512 像素的分析图不因模糊分自动判废。
- 人工星级、保留/排除和人工场景修正属于用户决策。旧库升级前备份；模型或质量规则变化仅重算派生指标，不应清掉人工决策。任何涉及 engine/pipeline.py 模型签名、engine/store.py 重算或迁移的修改，都要单独验证数据保护。
- 当前候选版的技术验收记录见[离线候选验收记录](docs/offline-quality-candidate.md)。Lightroom Classic 实机、真实摄影项目、PySide6/Qt 发行许可仍需按[人工验收清单](docs/acceptance/release-manual.md)执行。不要把历史测试数字当成当前通过结果。

## 目录与职责

| 路径 | 用途 |
| --- | --- |
| app_qt.py、styles.qss | 桌面入口与样式 |
| engine/ | 解码、质量与人脸信号、相似分组、推理、评分、SQLite 存储和分析管线 |
| services/ | 项目操作、复核、任务、导出、XMP 与发行冒烟服务 |
| ui/ | PySide6 项目入口、四阶段页面及复核组件 |
| tests/ | 正式 pytest 单元、UI、迁移、发行契约与端到端测试；不要作为临时文件清理 |
| scripts/ | 评测、基准、模型/许可/ExifTool 校验；generate/ 为测试数据生成器，diagnostics/ 为离线探针 |
| requirements/、requirements*.txt | 运行与开发依赖输入和锁定清单 |
| release/、models/、model_snapshots/ | 发行清单、许可文件、小权重和本地锁定模型快照 |
| docs/ | 当前候选版证据与待执行的人工验收清单 |
| data/ | 本地照片样本、研究数据集、人工标注和临时项目库；整体不进入 Git |
| dist/ | 本地候选 onedir 与 ZIP；可运行但不进入 Git |
| _archive/ | 仅本机保留的旧计划、研究和实验记录；不进入 Git |

源码的主要流程为：扫描与解码 → 技术质量、人脸、pHash → CLIP 美学与场景 → 相似分组与评分 → SQLite → 人工复核 → 复制导出。engine/、services/、ui/ 维持现有边界；这次目录清理没有改变业务模块接口。

## 开发环境与启动

以下命令在克隆后的仓库根目录运行。当前依赖锁以 Python 3.12 为基线；GPU 可选。已有 `.venv` 可直接使用：

~~~powershell
.\.venv\Scripts\python.exe app_qt.py
~~~

在新机器上创建环境：

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
~~~

若需要 CUDA 版 Torch，应选择与本机驱动匹配的 wheel，并重新验证依赖锁和候选包；不能把另一个 Torch 构建视作当前已验收包。

## 测试与诊断

~~~powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m pytest -q -o addopts=
.\.venv\Scripts\python.exe scripts\check_release_licenses.py --requirements requirements.txt --policy release\license-policy.toml
.\.venv\Scripts\python.exe scripts\diagnostics\cache_verify.py
.\.venv\Scripts\python.exe scripts\diagnostics\offline_probe.py
.\.venv\Scripts\python.exe scripts\smoke_dist.py --exe dist\光影选片助手\光影选片助手.exe
~~~

`pytest.ini` 默认排除较慢的 e2e；第二条命令运行完整测试。UI 测试需要 offscreen。离线诊断需要本地模型缓存，最后一条冒烟命令还需要本地构建的 `dist/` 候选包；新克隆的仓库没有这些文件。测试临时目录必须可读写；若 pytest 在建立 `tmp_path` 时报告 `PermissionError`，应先修正运行环境，再判断代码是否失败。所有测试和诊断结果以**当次输出**为准。

**2026-10-20 本机核验**：完整 pytest 为 536 passed、2 条第三方弃用警告；67 个发行依赖的许可开发检查通过；离线缓存校验、空缓存降级探针及保留 onedir 的禁网分析/导出冒烟均通过。正式发行所需的人工验收仍未完成。

生成本地演示、合成和性能数据：

~~~powershell
.\.venv\Scripts\python.exe scripts\generate\make_test_data.py
.\.venv\Scripts\python.exe scripts\generate\make_demo_data.py
.\.venv\Scripts\python.exe scripts\generate\make_perf_data.py 1000
~~~

make_demo_data.py 会重建 data/demo；make_perf_data.py 会重建 data/perf。运行前确认目标目录没有要保留的人工内容。真实素材、研究数据集和场景标注分别在 data/real、data/_datasets、data/annotations/场景标签.csv；标注 CSV 保持原始编码，评测脚本可读取 UTF-8 或 GBK。研究数据集只用于本地开发和评测，不随发行包分发。

## 候选包与发行边界

本机保留了候选 onedir 和 ZIP，均位于 Git 忽略的 `dist/`；GitHub 仓库不提供可下载的二进制包。复制 onedir 时必须保留整个文件夹及 `_internal`。发行规格为 `光影选片助手_dist.spec`，构建入口为 `build_dist.bat`；构建前须备齐并校验 ExifTool 与锁定模型快照，脚本还会运行完整测试与许可检查。当前不制作正式安装程序、GitHub Release 或版本标签。

与发行相关的入口：

- [离线候选验收记录](docs/offline-quality-candidate.md)：质量、场景和独立目录冒烟记录。
- [发行人工验收清单](docs/acceptance/release-manual.md)：仍待执行的 Lightroom、真实项目与许可验收。
- [人工测试说明](docs/acceptance/人工测试说明_v0.7.md)：执行时填写实际版本和结果。
- [更新日志](CHANGELOG.md)：已记录版本的历史变更；旧研究与计划仅保存在本机 `_archive/` 或 Git 历史中。

## 许可

项目源码采用 [Apache-2.0](LICENSE)。第三方组件与模型的归属及使用条件见 [NOTICE](NOTICE.md) 和 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)；PySide6/Qt 二进制发行还需完成上述人工许可验收。

## 后续开发约定

1. 优先完成真实 Lightroom/RAW/XMP 和 Qt 许可人工验收；正式发布前重复干净机离线验证。
2. 为四类场景补齐独立人工样本，尤其“其他”类；未达到验收门槛前保持自动场景中性权重。现有 49 张开发标注已用于方案选择，不能充当独立验收集。
3. 后续若研究组内相对排序或人物检索，先建立可复跑的基线与真值；模型、库结构和重算路径变化必须证明人工决策仍完整。
4. 代码、测试和文档放入对应受控目录；运行日志、照片、模型缓存、构建结果及临时库留在 Git 忽略目录。不改用户原片，不增加遥测或未要求的网络访问。
