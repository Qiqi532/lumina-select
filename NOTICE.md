# NOTICE — 第三方组件、模型与发行门槛

Lumina Select 源代码当前按仓库 `LICENSE` 中的 Apache-2.0 条款提供。第三方库、
Qt 组件、ExifTool 和模型权重仍受各自条款约束；自动检查是工程门禁，不构成法律意见。

## 发行约束（v0.6）

- `pyiqa 0.1.16` 的许可包含 PolyForm Noncommercial 限制。v0.5 已从运行依赖、
  技术质量评分路径和 PyInstaller hidden imports 中移除 pyiqa；`release/license-policy.toml`
  将其列为 `deny_packages`，发行包不得包含其代码或权重。
- **本项目自 v0.6 起只发行一个版本**（标准版 Torch/CLIP/ViT）。轻量版（OpenCV 启发式后端）
  及其独立安装器已删除，因此不再存在"两版许可清单"；轻量依赖锁
  `requirements-lightweight.txt` 与 `requirements/lightweight.in` 同步删除，现在只有
  `requirements.txt` 一份锁定清单供本文件的清单生成使用。
- **PySide6、PySide6-Essentials 与 shiboken6 属于人工复核包**。三者均为 Qt 官方的
  **LGPLv3** 组件，不再有 PyQt6 的 GPLv3 传染问题，因此本项目自有代码得以保留
  Apache-2.0。发行时必须满足 LGPL 义务：随包附 LGPLv3 全文、在 `THIRD_PARTY_NOTICES.md`
  声明、并**保持 onedir 形态**以保证 Qt 库可替换（禁止 onefile）。
- 精确 Python 包版本和安装元数据由 `scripts/check_release_licenses.py` 生成到
  `THIRD_PARTY_NOTICES.md`。正式发布还必须通过该脚本的 `--release` 门禁。
  v0.6 收口时的开发检查结果：**67 packages 通过，`pyiqa` 缺席**，清单重新生成后与
  仓库内 `THIRD_PARTY_NOTICES.md` **逐字节一致**（即清单没有滞后于锁定依赖）。

## 随项目使用的模型与原生组件

| 组件 | 用途 | 来源 | 许可/复核状态 |
| --- | --- | --- | --- |
| openai/clip-vit-base-patch32 | 美学评分与场景分类 | OpenAI / Hugging Face | MIT；发布前复核模型卡与随包内容 |
| LAION-Aesthetics sa_0_4_vit_b_32_linear | 美学回归线性头 | LAION-AI | Apache-2.0；发布前复核权重来源 |
| dima806/closed_eyes_image_detection | 闭眼 ViT 分类 | Hugging Face | Apache-2.0；发布前复核模型卡 |
| MediaPipe Face Mesh / Face Detection | 人脸关键点与 EAR | Google | Apache-2.0 |
| rawpy / LibRaw | RAW 解码 | rawpy / LibRaw | MIT；LibRaw 采用 LGPL/CDDL 双许可 |
| lxml | RAW XMP sidecar XML 合并 | lxml project | BSD-3-Clause |
| ExifTool 13.59 | 导出副本的非 RAW 文件内 XMP | Phil Harvey | Artistic License 1.0 / GPL；Windows 归档 SHA-256 `44b512b25af500724ba579d0a53c8fc5851628b692dd5e5d94ae4a15c2cba9ec`，见 `release/exiftool.lock.json` |

模型权重**不随包分发**：本地缺少时由程序联网下载到项目内 `.hf_cache`（打包态为 exe 目录下的
`.hf_cache`）。本地权重完整时程序只读本地、不再联网（见 `README.md` 的「离线能力」）。
任何正式候选都应以实际打包内容重新生成第三方清单并完成许可人工复核。
当前安装器仅输出明确标记的开发候选包；尚未取得 Qt 许可批准，也未完成 Lightroom 实机验收。
