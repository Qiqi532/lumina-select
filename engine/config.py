# -*- coding: utf-8 -*-
"""
config.py —— 全项目可调参数唯一来源（MVP 配置集中化）

设计目标：
    1. 所有可调阈值 / 权重 / 模型名 / 分析尺寸集中在这里，带默认值与中文注释；
    2. engine 各模块只从本文件读取参数，不再散落硬编码常量；
    3. app（Streamlit 侧栏滑块）也读本文件，做到“改配置即生效、界面与引擎口径一致”。

修改方式：
    直接修改下方值即可；各模块启动时读取最新值。分析进行中修改不会影响
    正在进行的任务（下一次分析生效）。
"""
from __future__ import annotations

import os
import sys

# ===========================================================================
# 一、路径与目录（项目内所有缓存/数据，均不落 C 盘）
# ===========================================================================
# 根目录的双形态解析：
#   开发态    → 仓库根目录（engine/ 的上一级）
#   打包态    → exe 所在目录（onedir 下 dist\光影选片助手\）。
# 必须区分：PyInstaller onedir 打包后 __file__ 指向只读的 _internal，
# 且安装目录可能是 Program Files —— 数据库/日志/模型缓存等可写内容
# 一律放 exe 旁；只读的随包模型权重放 _internal（sys._MEIPASS）。
if getattr(sys, "frozen", False):
    PROJECT_ROOT = os.path.dirname(os.path.abspath(sys.executable))
    _BUNDLED_ROOT = getattr(sys, "_MEIPASS", PROJECT_ROOT)
else:
    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _BUNDLED_ROOT = PROJECT_ROOT

DATA_DIR = os.path.join(PROJECT_ROOT, "data")          # 数据目录（测试图 / 数据库）
DEFAULT_DB = os.path.join(DATA_DIR, "cull.db")         # 默认 SQLite 索引库
# 只读模型权重：开发态在 models/，打包态在 _internal/models（随包分发）
MODELS_DIR = os.path.join(_BUNDLED_ROOT, "models")
LOG_FILE = os.path.join(PROJECT_ROOT, "smart_cull.log")  # 统一日志文件

# ===========================================================================
# 二、图像格式与加载
# ===========================================================================
# 大众格式（默认处理）；RAW 仅在检测到扩展名且已安装 rawpy 时启用
JPEG_PNG_EXTS = {".jpg", ".jpeg", ".png"}
RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2", ".dng", ".pef", ".sr2"}

LOAD_MAX_SIZE = 512        # 分析用图最大边长（质量/人脸/场景共用，避免反复解码）
PHASH_SIZE = 256           # pHash 计算用图最大边长
THUMB_DEFAULT_SIZE = 320   # 缩略图默认尺寸（app 展示）
THUMB_QUALITY = 88         # 缩略图 JPEG 质量
THUMB_MAX_SIZE = 1000      # 大图对比展示尺寸（app 复核页）
# 缩略图缓存目录：必须在【项目内】，绝不能写进用户照片目录（会污染图库、被网盘同步）
THUMB_CACHE_DIR = os.path.join(DATA_DIR, ".thumbcache")

# ===========================================================================
# 三、质量检测（quality.py）
# ===========================================================================
BLUR_WASTE_THRESHOLD = 60.0    # 拉普拉斯方差 < 60 视为严重模糊（废片）
EXPO_WASTE_RATIO = 0.5         # 过曝/欠曝像素占比 > 0.5 视为严重
BLUR_ANALYZE_SIZE = 512        # 拉普拉斯方差统一分析尺寸（保证可比）
QUALITY_RULE_VERSION = "blur-aspect-no-upsample-small-review-v1"
BRISQUE_ANALYZE_SIZE = 512     # BRISQUE 输入尺寸
BRISQUE_SATURATED = 80.0       # BRISQUE ≥80 高度怀疑非自然内容（插画/截图）→ 中性化保护
BRISQUE_WASTE_THRESHOLD = 50.0 # BRISQUE > 50 视为严重失真（仅作评分参考，不作废片触发源）

# --- 【v0.6 S2-1】推理后端选择已移除 --------------------------------------
# 旧配置（INFERENCE_BACKEND / 环境变量 LUMINA_INFERENCE_BACKEND）在 v0.6 删除：
#   本项目自 v0.6 起只发行标准版（Torch CLIP/ViT），轻量 heuristic 后端与
#   双版本机制一并下线。后端现在是**调用参数**（analyze_directory(..., backend=...)），
#   默认值唯一来源是 engine/inference.py::DEFAULT_BACKEND，不再放在 config 里 ——
#   用全局配置切换后端会让"谁改了后端"不可追踪，历史上已因此丢过人工复核数据。

# --- 【模型升级 v0.4】美学模型（真实照片优先）-------------------------------
# 注意：musiq-ava 作为美学模型目前是【预留配置，尚未接入】—— aesthetics.py 当前
# 实现为 LAION 线性头（laion-head）→ CLIP 提示词（clip-prompt）两级降级。
# 原因：AVA 监督模型（musiq-ava）在画质基准上表现更好，但其分数量纲与 LAION 头
# 不同，接入需同步调整 scorer 权重与旧库迁移逻辑，见 CHANGELOG 的跟进计划。
AESTHETIC_MODEL = "laion-head"            # 当前生效：LAION 线性头
AESTHETIC_FALLBACKS = ["clip-prompt"]     # 头部缺失/加载失败时降级
AESTHETIC_ANALYZE_SIZE = 512              # 美学模型输入边长

# ===========================================================================
# 四、相似聚类（similarity.py）
# ===========================================================================
BURST_INTERVAL_MS = 1500.0     # 连拍时间间隔阈值：相邻拍摄时间 < 1500ms 归入同一连拍组
PHASH_THRESHOLD = 12           # pHash 汉明距离 < 12 视为相似
PHASH_HASH_SIZE = 8            # pHash 位数（8x8=64 位）

# ===========================================================================
# 五、人脸与闭眼（faces.py）
# ===========================================================================
EAR_CLOSED_THRESHOLD = 0.20    # EAR < 0.20 视为闭眼（明显闭眼）
FACE_NEUTRAL_N = 0.5           # 检测不到人脸时 face 维度取中性值（不做闭眼硬判）
# --- 【修复 v0.4】EAR 归一化区间 -------------------------------------------
# 原实现 norm_face = clip(EAR / 0.35)：实测睁眼 EAR 约 0.30~0.45，0.36 即打满 1.0，
# 导致"睁得大"和"刚睁开"无法区分；闭眼 EAR 约 0.05~0.20 却仍能有 0.14~0.57 分，
# 区分度严重不足。改为 [EAR_CLOSED_REF, EAR_OPEN_REF] 线性区间映射 + 截断。
EAR_CLOSED_REF = 0.10          # EAR ≤ 此值 → face_n = 0
EAR_OPEN_REF = 0.32            # EAR ≥ 此值 → face_n = 1
# 闭眼硬判是否依赖场景分类。
# 【修复 v0.4】原实现仅当 scene == "人像" 才判闭眼废片，而 CLIP 场景分类会把
# 不少人脸照判成"其他/风光"（实测 face_closed1.jpg → 其他 0.410），
# 一旦判错，闭眼检测（本工具的核心卖点）就整体失效。
# 改为：只要【确实检测到人脸】就启用闭眼硬判，与场景分类解耦。
EYE_WASTE_REQUIRE_PORTRAIT_SCENE = False
# 闭眼 ViT 分类器（dima806，Apache-2.0，输入为眼睛区域裁剪图）
EYE_MODEL_NAME = "dima806/closed_eyes_image_detection"
EYE_MODEL_CONF = 0.60          # 分类器闭眼置信度阈值（高于则判闭眼）
EYE_CROP_CONTEXT = 1.2         # 眼睛裁剪上下文放大系数
EYE_MODEL_EAR_LO = 0.15        # 分类器触发区间下限（明显睁眼 EAR≥0.33 不跑分类器）
EYE_MODEL_EAR_HI = 0.33        # 分类器触发区间上限（明显闭眼 EAR<0.15 不跑分类器）

# ===========================================================================
# 六、美学与场景（aesthetics.py / scene.py）
# ===========================================================================
# CLIP 美学提示词（仅当 LAION 美学头缺失/加载失败时作为降级方案）
AESTHETIC_GOOD_PROMPTS = [
    "a beautiful professional photo, sharp, well composed, great lighting",
    "a high-quality award-winning photograph",
]
AESTHETIC_BAD_PROMPTS = [
    "a blurry amateur snapshot",
    "a poorly composed ugly photo",
]
# 场景候选（取空格前单词作为场景名）
SCENES = [
    "人像 portrait",
    "风光 landscape",
    "建筑/城市 architecture",
    "街拍/纪实 street",
    "宠物 pet",
    "静物/美食 still life",
    "其他 other",
]
SCENE_LABELS = ("人像", "风景", "街拍", "其他")
SCENE_ALIASES = {
    "人像": "人像", "风光": "风景", "风景": "风景",
    "建筑/城市": "风景", "街拍/纪实": "街拍", "街拍": "街拍",
    "宠物": "其他", "静物/美食": "其他", "其他": "其他",
}
SCENE_RULE_VERSION = "four-scene-map-v1"
SCENE_CONF_THRESHOLD = 0.6     # 低置信度仅标记待确认，不改写候选类别
SCENE_ADAPTIVE_CLASSES: tuple[str, ...] = ()  # 独立人工样本验收前保持中性权重


def scene_label(value: str) -> str:
    """将旧版七类名称映射到摄影师使用的四类标签。"""
    return SCENE_ALIASES.get(value, value)
CLIP_MODEL_NAME = "openai/clip-vit-base-patch32"   # 美学 + 场景共用模型
# LAION-Aesthetics 线性头（基于 openai/clip-vit-base-patch32 图像嵌入，约 3KB，随项目分发）
AESTHETIC_HEAD_PATH = os.path.join(MODELS_DIR, "sa_0_4_vit_b_32_linear.pth")

# ===========================================================================
# 七、评分与甄选（scorer.py）
# ===========================================================================
# 四类场景预设：街拍先沿用均衡权重，不新增未经验证的评分参数。
SCENE_PRESETS = {
    "人像": {"w": (0.25, 0.20, 0.25, 0.30), "check_eye": True, "label": "人像"},
    "风景": {"w": (0.35, 0.30, 0.35, 0.00), "check_eye": False, "label": "风景"},
    "街拍": {"w": (0.30, 0.20, 0.30, 0.20), "check_eye": True, "label": "街拍"},
    "其他": {"w": (0.30, 0.20, 0.30, 0.20), "check_eye": True, "label": "其他"},
}
# --- 【修复 v0.7 / R-DEGRADE-1】场景**未知**时的中性权重 -----------------------------
# 什么算"未知"：整组都没有真实场景来源 —— 既无本次 CLIP 结果，也无库中历史场景、
# 无人工修正、无强制场景（典型情形：CLIP 后端降级不可用）。
#
# 为什么不能复用 "其他" 预设：`resolve_scene()` 在缺失信号时**兜底返回 "其他"**，
# 于是"没有信息"被当成"分类结果是其他"来用权重。这会带来两个问题：
#   1) 语义混淆：把"未知"伪装成"已分类为其他"，一次 CLIP 偶然可用 / 不可用，
#      同一张照片会拿到不同权重，而数据里看不出哪种情况发生过；
#   2) 实测可让**最佳帧推荐翻转**：例如人像连拍组在降级时被按 "其他" 评分，
#      X(脸好/略糊) 与 Y(脸差/更清晰) 的胜者会随权重改变（人像权重选 X，其他权重选 Y）；
#      风光连拍组同理（"其他" 与 "风光" 对 blur/expo 的相对权重不同，也会翻转）。
#      回归用例见 tests/test_pipeline.py::test_degraded_run_scores_with_neutral_weights*。
#
# 取值 = **三套不同权重档位的算术平均**（街拍暂与其他共用均衡档位）：
#   blur (0.25+0.35+0.30)/3 = 0.30000
#   expo (0.20+0.30+0.20)/3 = 0.23333
#   aes  (0.25+0.35+0.30)/3 = 0.30000
#   face (0.30+0.00+0.20)/3 = 0.16667   ← 保留人脸维度
# 刻意**保留** face 维度而非置 0：人脸与睁眼度是真实测得的信号（MediaPipe），
# "不知道是什么场景"不等于"不知道有没有人脸"；置 0 会白白丢掉本就稀缺的可用信息。
UNKNOWN_SCENE_WEIGHTS = (0.30, 0.70 / 3.0, 0.30, 0.50 / 3.0)
BLUR_WASTE = BLUR_WASTE_THRESHOLD   # 严重模糊阈值（与质量检测同源）
EXPO_WASTE = EXPO_WASTE_RATIO       # 严重过曝/欠曝占比
EAR_WASTE = EAR_CLOSED_THRESHOLD    # 闭眼 EAR 阈值（与 faces 同源）
BRISQUE_WASTE = BRISQUE_WASTE_THRESHOLD  # BRISQUE 严重失真参考阈值
SCORE_GAP = 3.0                     # Top1-Top2 综合分差 < 3 → 无明确胜者 → 待甄选
SCENE_CONF_LOW = SCENE_CONF_THRESHOLD  # 场景置信度阈值（< 则待甄选）
DIM_GAP = 0.05                      # 维度冲突判定：两候选维度差距 < 0.05 且互有胜负
MAX_CANDIDATES = 5                  # 待甄选候选最多 5 张
FACE_NEUTRAL = FACE_NEUTRAL_N       # 无脸时 face 维度中性值

# --- 【修复 v0.4】重复判定策略 ---------------------------------------------
# 原实现：组内 Top2 之外的所有照片一律打上「高度重复」废片标记。
#   组大小 3 → 1 张判废；5 → 3 张；10 → 8 张；20 → 18 张。
#   问题：这只是"排名落败"，不是"重复"。一张构图不错、只是略逊于同伴的照片会被
#   标成废片并排除出导出清单，摄影师会因此丢失可用素材。
# 新策略：落选者只标记为「相似·落选」（不影响废片判定、默认收起但仍可找回），
#   只有与最佳帧 pHash 汉明距离极近（真·近乎像素重复）才判为「高度重复」废片。
DUP_HAMMING_STRICT = 4              # pHash 距离 ≤ 4 才视为"高度重复"（64 位中 ≤4 位不同）

# ===========================================================================
# 八、流水线（pipeline.py）
# ===========================================================================
ANALYZE_SIZE = LOAD_MAX_SIZE   # 分析用图尺寸（质量/人脸）
CLIP_BATCH = 16                # CLIP 批量推理大小（GPU 友好）
CLIP_LOAD_SIZE = 256           # CLIP 阶段重新解码用图边长（CLIP 内部缩到 224，256 足够）
QUALITY_WORKERS = 4            # 质量检测线程池并行数（纯 cv2 计算，GIL 释放，可并行）

# ===========================================================================
# 九、导出
# ===========================================================================
EXPORT_SUBDIR = "photos"       # 导出时复制文件的子目录名
EXPORT_CSV_NAME = "smart_cull_export.csv"  # 导出 CSV 清单文件名

# ===========================================================================
# 十、其他
# ===========================================================================
# 分析阶段名（进度回调与断点续跑状态共用）

# ===========================================================================
# 十一、【修复 v0.4】模型加载健壮性
# ===========================================================================
# 网络/缓存故障是本项目实测到的头号崩溃来源：
#   * HuggingFace 缓存的 .no_exist 标记会随 transformers 升级而过期
#     （本机实测：旧版写入 processor_config.json 不存在，transformers 5.x 却需要它
#       → 离线加载 CLIP 直接 OSError 崩溃，GUI 只弹"分析出错"）
#   * 代理/断网时下载失败会抛出原始异常，用户完全无法自救
# 对策：缓存自愈 + 离线优先 + 失败可解释。
OFFLINE_FIRST = True           # 本地缓存存在时优先离线加载，避免网络抖动阻塞启动
MODEL_LOAD_TIMEOUT = 300       # 单个模型加载超时（秒），超时后走降级链
# HuggingFace 镜像端点（国内网络可直接下载时留空；被墙时设为 https://hf-mirror.com）
HF_ENDPOINT = "https://hf-mirror.com"
