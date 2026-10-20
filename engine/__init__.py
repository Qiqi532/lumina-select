# Lumina Select · 光影选片助手 核心引擎包
# 纯 Python 实现，无 UI 依赖，可独立命令行调试；未来可直接迁移桌面端。
"""
光影选片助手核心引擎（engine）

模块说明：
    config.py      全项目可调参数唯一来源（阈值/权重/模型名/路径，界面与引擎同源）
    log.py         统一日志（控制台 + 文件）
    loader.py      目录扫描、JPEG/PNG/RAW 解码、EXIF、缩略图
    quality.py     模糊、曝光检测（拉普拉斯方差 / 直方图占比）+ 可插拔画质模型(IQA)
    models_guard.py 模型缓存自愈 + 离线优先 + 多级回退链（CLIP/IQA/闭眼分类器）
    faces.py       MediaPipe 人脸检测与闭眼 EAR + ViT 分类器融合
    aesthetics.py  CLIP 美学评分（LAION 线性头，缺失时降级 CLIP 提示词，GPU 优先）
    scene.py       CLIP 场景分类（人像/风景/街拍/其他，GPU 优先）
    similarity.py  pHash 相似、连拍分组、并查集聚类（流式 O(block×n) 内存有界）
    scorer.py      场景自适应评分、废片判定、最佳帧 + 不确定甄选
    store.py       SQLite 读写（WAL + 事务化 + 断点续跑辅助）
    pipeline.py    端到端编排（分块流式内存控制、线程池、断点续跑、增量、进度回调）
"""

__version__ = "0.4.0"
__version_info__ = (0, 4, 0)
