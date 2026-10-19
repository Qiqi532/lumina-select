# 离线选片开发候选版：质量与场景验收记录

本候选版面向 JPEG、PNG、RAW 的本地选片与人工复核。它不承诺连拍最佳帧 Top-1 准确率；Lightroom 实机、HEIC、人物检索和正式发行仍在本轮范围之外。

## 规则与数据保护

- 质量图最长边大于 512 像素时按比例缩到 512，不再拉伸为正方形；小于 512 像素时不放大，模糊分仅供展示，不能触发自动判废或拉低清晰维度，整组进入待甄选。
- 新质量规则通过独立 `quality_rules` 元数据标记识别。旧项目重算前使用 SQLite backup API 生成 `.pre-quality.bak`，人工星级、保留／排除与人工场景修正仍保留。AI 模型签名语义未改变。
- 场景权重只允许独立人工样本达到逐类召回率至少 80%、整体准确率不低于旧版时按类启用。目前 `SCENE_ADAPTIVE_CLASSES` 为空；自动场景只作为展示建议，评分使用三预设均值，组进入待甄选。人工指定场景与显式自定义权重仍有效。
- 降级事件分为 `info`、`warning`、`critical`；CPU 回退为信息，闭眼 ViT 缺失为警告，CLIP 阶段不可用为严重。未知场景置信度为 0。

## 可复跑评测

| 数据与口径 | 当前提示词 | 英文照片描述提示词 | 结论 |
| --- | ---: | ---: | --- |
| RAISE 66 张可比 RAW；三评分类，阈值 0.6 | 39/66（59.1%） | 37/66（56.1%） | 保留当前提示词 |
| RAISE 同批；七展示标签 | 22/66（33.3%） | 23/66（34.8%） | 七标签仍只作展示 |
| 摄影师人工标注 49 张 JPEG；三评分类 | 14/49（28.6%） | 7/49（14.3%） | 两方案均不足以启用自适应权重 |

人工样本含人像 29 张、风光 20 张、其他 0 张；当前提示词的人像召回 5/29（17.2%）、风光召回 9/20（45%）。缺少“其他”真值，不能推断该类召回率。该样本不是连拍首选标注，不用于声称最佳帧准确率。英文提示词在 RAISE 和人工样本的三类表现均低于当前提示词；阈值网格只用于诊断，不在这批独立样本上挑选新阈值。

旧尺寸实验中，6 张 RAW 的 256 像素版本有 4 张触发模糊自动判废，原尺寸 0 张。新规则在 1024、512、256、128 像素四档的自动模糊判废均为 0/6；256 和 128 像素档因信号不可靠转人工确认。这个结果证明误剔除路径被阻断，不证明模糊分类本身达到某个准确率。

本地复跑命令（评测输出及照片均留在被 Git 忽略的 `data/` 或用户目录）：

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_blur_sizes.py --glob 'data/_datasets/raise/raw/*.NEF' --limit 6 --output data\_blur_sizes_candidate.json
.\.venv\Scripts\python.exe scripts\evaluate_scenes.py --manifest data\_datasets\raise\_manifest\raise_selected.csv --raise-root data\_datasets\raise\raw --output data\_scene_raise_candidate.json
.\.venv\Scripts\python.exe scripts\evaluate_scenes.py --manifest 'D:\PHOTO\Photo like\场景标注\场景标签.csv' --output data\_scene_holdout_result.json
```

人工标注 CSV 可由 `scripts/prepare_scene_labels.py` 重建；可接受 UTF-8 或 Excel 在中文 Windows 上保存的 GBK 编码，“风景”会映射为“风光”。重新标注时应保留未参与提示词选择的独立样本，尤其补足至少 8 张“其他”。

## 离线包与复核门禁

`release/models.lock.json` 固定 CLIP 与闭眼 ViT 的仓库修订、来源、大小和逐文件 SHA-256。`scripts/stage_models.py` 只从完整快照复制清单文件；冻结版只从 `_internal/model_snapshots` 加载。两套权重和照片都不进入 Git。包内权重缺失或校验失败时，不读取用户缓存、不静默联网，界面显示降级，场景未知时不给高置信最佳帧。

发行前检查包括完整 pytest、ExifTool 与第三方许可检查、PyInstaller 构建、独立目录且空模型缓存的禁网冒烟，以及便携 ZIP 的 SHA-256。Lightroom 实机和许可人工复核继续按 `docs/v0.5-manual-acceptance.md` 进行。本次交付保留开发候选状态。

2026-10-19 的本机验收：完整 pytest 为 **521 passed、2 条第三方弃用警告**；发行依赖许可检查通过 67 个包，发行契约测试 12 项通过。最终 PyInstaller onedir 重建成功，约 1.86 GiB。原目录与复制到 `data/_candidate_detached_20261019` 的目录均在空用户模型缓存、离线模式下完成启动、CLIP 和闭眼 ViT 包内加载、分析及导出，未出现降级。复制品临时移走 CLIP `config.json` 后，程序明确报告 `missing or wrong size: config.json`，冒烟返回非零；随后恢复该文件。

真实照片副本验收导入并分析 1 张 RAISE RAW、由该 RAW 生成的同名 JPEG、1 张人工标注 JPEG 和由该 JPEG 生成的 PNG，共 4 个文件；RAW+JPEG 配对、人工选择、撤销、再次选择及 RAW+JPEG 导出均通过。原始照片与工作副本在分析和导出前后的 SHA-256 不变。具体本地输出保存在被 Git 忽略的 `data/_real_acceptance/report.json`。

## 性能边界与后续工作

本机复核基准：1,000 项载入 0.027 秒、5,000 项载入 0.135 秒；分面组合分别最高约 0.014 秒和 0.031 秒，均低于脚本的 0.3 秒门槛。这个基准涵盖 SQLite、复核服务、分面计算和筛选条控件绘制；不包含大图解码与完整窗口滚动帧率，后者仍需现场观察。

下一轮优先获取不与本次调参共用的三类人工样本，补足“其他”，再研究人脸信号与 CLIP 场景融合、拒判校准和摄影师连拍首选标签。任何自适应权重启用都要重新通过逐类召回与整体指标门槛；现有中性权重路径应作为可恢复的保守基线。
