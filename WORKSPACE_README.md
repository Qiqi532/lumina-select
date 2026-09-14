# Lumina Select v0.5 工作区说明

最后同步：2026-09-14
当前分支：`lightweight`
远端状态：`origin/lightweight` 已同步到提交 `0f5921b`

## 当前进度

- v0.5 专业选片流程已实现：导入、分析、复核、导出到 Lightroom。
- 轻量版使用 OpenCV 启发式评分，不包含 Torch、Transformers 或模型下载。
- 标准版保留 Torch/CLIP/ViT 与 OpenCV 技术质量评分。
- RAW+JPEG 按同一资产关联，人工决定同步；RAW 使用 XMP sidecar，非 RAW 只写导出副本。
- 已修复 Qt6 复核绘制错误、RAW 分析卡顿、人脸检测冻结打包问题，以及中文路径下 ExifTool 导出失败。
- 你提供的 `D:\PHOTO\SYSU\South\26.9.8` 已验证：87 张 NEF、36 张 JPG 均可读取和生成预览。
- 尚未创建正式 GitHub Release；PyQt6/Qt 发行许可和 Lightroom 实机验收仍需人工完成。

## 启动文件

最新开发候选安装包：

- `build-tools\Output-light-stage-r8\LuminaSelect-v0.5-lightweight-dev-candidate.exe`
- `build-tools\Output-standard-stage-r8\LuminaSelect-v0.5-standard-dev-candidate.exe`

无需安装的 onedir 程序：

- `build-tools\dist-light-r8\光影选片助手\光影选片助手.exe`
- `build-tools\dist-standard-r8\光影选片助手\光影选片助手.exe`

建议先使用轻量版；标准版需要本地 CLIP 缓存或联网下载模型。

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
| `.venv-light/` | 轻量开发/测试环境 |
| `.venv/` | 标准开发/测试环境 |
| `.hf_cache/`、`.torch_cache/` | 本地模型缓存，不提交 Git |
| `data/` | 测试图片、性能数据和本地数据库；不写入用户原片目录 |

## 常用验证命令

```powershell
$env:LUMINA_INFERENCE_BACKEND='heuristic'
$env:QT_QPA_PLATFORM='offscreen'
.\.venv-light\Scripts\python.exe -m pytest -q -o 'addopts='
.\.venv\Scripts\python.exe -m pytest -q -o 'addopts='
.\.venv-light\Scripts\python.exe scripts\benchmark_review.py --items 1000
```

PyInstaller 规格：`光影选片助手_dist_lightweight.spec` 和 `光影选片助手_dist.spec`。
安装包脚本：`installer_lightweight.iss` 和 `installer.iss`。

## Git 注意事项

- 受保护文件 `评审总览_v0.4.md` 必须保持未跟踪，不得暂存或提交。
- 构建产物、虚拟环境、缓存、日志和数据库由 `.gitignore` 排除。
- 目前最近三个修复提交：`4346d7c`、`572bcd6`、`0f5921b`。
