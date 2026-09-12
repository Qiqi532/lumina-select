# NOTICE — 第三方组件、模型与发行门槛

Lumina Select 源代码当前按仓库 `LICENSE` 中的 Apache-2.0 条款提供。第三方库、
Qt 组件、ExifTool 和模型权重仍受各自条款约束；自动检查是工程门禁，不构成法律意见。

## v0.5 发行约束

- `pyiqa 0.1.16` 的许可包含 PolyForm Noncommercial 限制。v0.5 已从标准/轻量
  运行依赖、技术质量评分路径和 PyInstaller hidden imports 中移除 pyiqa；发行包不得
  包含其代码或权重。
- PyQt6、PyQt6-Qt6 与 PyQt6-sip 属于人工复核包。没有真实、可审计的商业许可或
  GPLv3 兼容发行决定时，只能生成清晰标记的开发候选包，不得声称正式发布完成。
- 精确 Python 包版本和安装元数据由 `scripts/check_release_licenses.py` 生成到
  `THIRD_PARTY_NOTICES.md`。正式发布还必须通过该脚本的 `--release` 门禁。

## 随项目使用的模型与原生组件

| 组件 | 用途 | 来源 | 许可/复核状态 |
| --- | --- | --- | --- |
| openai/clip-vit-base-patch32 | 标准版美学评分与场景分类 | OpenAI / Hugging Face | MIT；发布前复核模型卡与随包内容 |
| LAION-Aesthetics sa_0_4_vit_b_32_linear | 标准版美学回归线性头 | LAION-AI | Apache-2.0；发布前复核权重来源 |
| dima806/closed_eyes_image_detection | 标准版闭眼 ViT 分类 | Hugging Face | Apache-2.0；发布前复核模型卡 |
| MediaPipe Face Mesh / Face Detection | 两版人脸关键点与 EAR | Google | Apache-2.0 |
| rawpy / LibRaw | RAW 解码 | rawpy / LibRaw | MIT；LibRaw 采用 LGPL/CDDL 双许可 |
| lxml | RAW XMP sidecar XML 合并 | lxml project | BSD-3-Clause |
| ExifTool | 导出副本的非 RAW 文件内 XMP | Phil Harvey | Artistic License 1.0 / GPL；版本与 SHA-256 另行锁定 |

标准版允许在本地缺少模型时下载 CLIP/ViT 权重；轻量版不包含 Torch、Transformers
或模型下载。任何正式候选都应以实际打包内容重新生成第三方清单并完成许可人工复核。
