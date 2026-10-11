# Lumina Select 工作区说明

最后同步：2026-10-11
当前分支：`lightweight`（注意：此分支名**名不符实**，它实际是开发主线）
远端状态：`origin/lightweight` 停在 `0832f7a`；本地已新增 v0.6 的 S1/S2/S3 三个提交（**未推送**）；`master` 停在 `37332c4`（v0.4.0 回溯点）

> **✅ v0.6「单版本基线」代码与文档已收口**：S1 数据保护（门禁 G1 通过）、S2 双版本收敛、S3 加固层、S4 迁移与文档层全部落地；本地提交齐备、每步测试全绿，**尚未推送远端**。
> 完整计划、门禁与风险清单见 **`../开发计划_v0.6-v0.8_2026-10-10.md`**。
> **轻量版已删除**（后端、配置开关、打包四件套、依赖锁、两版对比断言），不要再基于它开发。
> **旧项目可直接打开**：schema 升级先自动备份、指标按需重算、人工复核零丢失，并可用「导出已复核清单」留一份 CSV 兜底。

> **分支策略已定（GH-1 选项①）**：`lightweight` 合并/改名为 `main`，`master` 留作回溯点。
> **仅为记录，未执行任何推送**——按约定，所有 GitHub 动作须逐次获得用户授权。

## 当前阶段：v0.6「单版本基线」

原硬性约束「先修 P0-1，再做合并」**已满足**：`engine/pipeline.py` 在模型签名变化时不再 `clear_all()`，
而是 `reset_metrics()`（只置空模型派生列、保留 `star`/`label`/`decision_source`/`scene_manual`）；
换分析源时先进 `meta` 归档人工决策、再清库、再恢复。数据保护回归（Repro A/B/C）全绿，
并有 `tests/test_pipeline.py` 的两条 P0-1 回归用例长期守住。
S2-1 进一步删除了进程级后端开关 `config.INFERENCE_BACKEND`——P0-1 的**根因**随之消失。

## 当前进度

- v0.5 专业选片流程已实现：导入、分析、复核、导出到 Lightroom。
- **只剩一条产品线**：Torch/CLIP/ViT + OpenCV 技术质量评分（轻量版整条发行线已删除）。
- RAW+JPEG 按同一资产关联，人工决定同步；RAW 使用 XMP sidecar，非 RAW 只写导出副本。
- 已修复 Qt6 复核绘制错误、RAW 分析卡顿、人脸检测冻结打包问题，以及中文路径下 ExifTool 导出失败。
- 你提供的 `D:\PHOTO\SYSU\South\26.9.8` 已验证：87 张 NEF、36 张 JPG 均可读取和生成预览。
- 尚未创建正式 GitHub Release；PyQt6/Qt 发行许可和 Lightroom 实机验收仍需人工完成。

## 启动文件

最新开发候选安装包：

- `build-tools\Output-standard-stage-r8\LuminaSelect-v0.5-standard-dev-candidate.exe`

无需安装的 onedir 程序：

- `build-tools\dist-standard-r8\光影选片助手\光影选片助手.exe`
- 本次 v0.6 S2 重新构建的产物：`dist\光影选片助手\光影选片助手.exe`

收敛后只有这一个产品线，需要本地 CLIP 缓存或联网下载模型。
历史轻量版产物（`build-tools\Output-light-stage-r8\`、`build-tools\dist-light-r8\`）仅作 v0.5 对照，**不再更新、可随时删除**。

## v0.6 收口要点（S4）

- **旧库升级是明示的**：schema 升级前自动备份到 `<库名>.pre-v05.bak`；只有确实检测到旧库时，
  状态行才会出现「旧库升级说明」按钮（新项目看不到，避免噪音）。
- **重算不丢人工决策**：模型签名变化 / 旧后端产物 → `reset_metrics()` 只清度量列，
  分析完成时状态行写明「指标已重算 + 人工复核 N 条已保留」。
- **导出兜底**：状态行「导出已复核清单」把人工决策导出为 CSV（UTF-8 BOM，Excel 可直接打开）。
- 相关测试：`tests/test_upgrade_v06.py`（门禁 G4）、`tests/test_migrations.py`（备份与回滚）。

## 目录结构

| 路径 | 用途 |
| --- | --- |
| `engine/` | 扫描、RAW/JPEG 资产配对、评分、推理、数据库 |
| `services/` | 复核、导出、XMP、分发冒烟服务 |
| `ui/` | PyQt6 界面、分析页、复核页、胶片带、检查器 |
| `tests/` | 单元、UI、发布工具和端到端测试 |
| `release/` | 许可证策略、ExifTool 锁定运行时和配置 |
| `models/` | 随仓库分发的小权重；大模型在缓存目录 |
| `dist*/` | 历史或当前可直接运行的 onedir 程序，清理时保留 |
| `build-tools/Output-*-stage-r8/` | 最新安装包，清理时保留 |
| `.venv/` | 唯一开发/测试环境（`.venv-light` 已随 S2 删除） |
| `.hf_cache/`、`.torch_cache/` | 本地模型缓存，不提交 Git |
| `data/` | 测试图片、性能数据和本地数据库；不写入用户原片目录 |
| `data/_offline_probe.py`、`data/_cache_verify.py` | 门禁 G3 的离线能力证据脚本 |

## 常用验证命令

**v0.6 S2 后只剩一条链路，无需预设任何后端变量：**

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests                      # 238 passed, 1 deselected（约 21s）
.\.venv\Scripts\python.exe -m pytest -q -o 'addopts='           # 239 passed（含 e2e，约 24s）
.\.venv\Scripts\python.exe scripts\benchmark_review.py --items 1000 --json benchmark-standard.json
.\.venv\Scripts\python.exe scripts\check_release_licenses.py    # 67 packages
.\.venv\Scripts\python.exe data\_cache_verify.py                # 离线加载全部运行期模型
.\.venv\Scripts\python.exe data\_offline_probe.py               # 断网+空缓存 → 降级完成而非崩溃
```

> 注：`pytest tests` 里的 `test_pipeline.py` 使用**注入的确定性测试后端**，不加载 torch/CLIP；
> 唯一会真正加载模型的是发布冒烟用例（约 4~15 s）。

PyInstaller 规格：`光影选片助手_dist.spec`（**唯一**发行规格）。
安装包脚本：`installer.iss`（**唯一**）。

## Git 注意事项

- 受保护文件 `评审总览_v0.4.md` 必须保持未跟踪，不得暂存或提交。
- 构建产物、虚拟环境、缓存、日志和数据库由 `.gitignore` 排除。
- 验证/复现脚本一律写 `data/`，不要落在仓库根目录。
- 远端仓库为 `https://github.com/Qiqi532/lumina-select.git`（非 `smart-cull-assistant`）。
- **推送前须经用户确认**；若遇 502，用 `git -c http.version=HTTP/1.1 -c pack.threads=1 push --no-thin origin <branch>`，成功判据为 `git ls-remote origin refs/heads/<branch>` 哈希已更新。
- 最近提交（v0.6）：S3 加固层 `07b824a`、S2 收敛层 `464797b`、S1 阻断层 `a89d66f`；其前为 `0832f7a`（文档）、`0f5921b`（Unicode 导出路径）、`572bcd6`（隔离候选输入）。
