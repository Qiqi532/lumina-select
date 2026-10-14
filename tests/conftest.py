# -*- coding: utf-8 -*-
"""pytest 公共夹具：构造微型测试图片与临时数据库。"""
from __future__ import annotations

import os
import shutil

import numpy as np
import pytest
from PIL import Image

# 强制无 GPU，保证单测在 CPU 上稳定快速
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")


@pytest.fixture(autouse=True)
def _isolate_global_registry(tmp_path_factory, monkeypatch):
    """把全局项目注册表 + 默认项目库隔离到临时目录。

    【T-05】MainWindow 默认会创建 ``CatalogRegistry()``（位置
    ``%LOCALAPPDATA%\\LuminaSelect\\catalogs.db``）并在启动时登记当前项目
    （默认 ``config.DEFAULT_DB`` = 仓库内 ``data/cull.db``）。若不隔离：
      * 跑一次测试就往**用户真实**的 %LOCALAPPDATA% 写入一堆 tmp 项目；
      * 仓库内 ``data/cull.db`` 被创建后，「默认库本来就存在」恒为真，
        入口页会多出一张幽灵卡片，污染与"项目数"有关的断言。
    这里把两者都指到会话级临时目录，测试之间互不干扰。
    """
    from engine import config

    fake_local = tmp_path_factory.mktemp("localappdata")
    fake_data = tmp_path_factory.mktemp("lumina_data")
    monkeypatch.setenv("LOCALAPPDATA", str(fake_local))
    monkeypatch.setattr(config, "DEFAULT_DB", str(fake_data / "cull.db"))
    yield fake_local


@pytest.fixture()
def tmp_db_path(tmp_path):
    """临时 SQLite 路径（每次测试独立）。"""
    return str(tmp_path / "test.db")


@pytest.fixture()
def photo_dir(tmp_path):
    """构造含多张可控图片的目录，返回目录路径。"""
    d = tmp_path / "photos"
    d.mkdir()

    def make(name, kind="flat", size=64, seed=0):
        rng = np.random.default_rng(seed)
        if kind == "flat":          # 纯色平滑 → 拉普拉斯方差低（易被判模糊）
            arr = np.full((size, size, 3), 128, dtype=np.uint8)
        elif kind == "noisy":       # 随机噪声 → 高拉普拉斯方差（清晰）
            arr = rng.integers(0, 256, (size, size, 3), dtype=np.uint8)
        elif kind == "white":       # 全白 → 过曝
            arr = np.full((size, size, 3), 255, dtype=np.uint8)
        elif kind == "black":       # 全黑 → 欠曝
            arr = np.zeros((size, size, 3), dtype=np.uint8)
        elif kind == "gray":        # 平滑灰 → 拉普拉斯低，BRISQUE 不应误判为严重失真
            arr = np.full((size, size, 3), 140, dtype=np.uint8)
        else:
            arr = rng.integers(0, 256, (size, size, 3), dtype=np.uint8)
        Image.fromarray(arr).save(d / name)
        return str(d / name)

    return d, make


@pytest.fixture()
def full_data_dir(tmp_path):
    """构造 end-to-end 冒烟用的数据目录（连拍/模糊/过曝/清晰）：
    返回 (目录, 各张路径 dict)。

    设计：
      - 写入真实 EXIF 拍摄时间，让“连拍分组”（主机制）确定性生效：
          burstA 三张（1.0s/1.2s/1.4s）、burstB 两张（20.0s/20.2s）为连拍组；
          其余单张时间互相远离（40s/50s/60s）。
      - 内容用结构化图案（棋盘/渐变/条纹）增加 pHash 区分度：
          burstA = 清晰棋盘原图 + 2 张高斯模糊版（组内最佳帧明确）；
          burstB = 两张近同渐变图（组内无明确胜者，进入待甄选）；
          blurry = 平滑灰（模糊废片）；overexposed = 纯白（过曝废片）；
          sharp  = 条纹图案（清晰，独立）。
    """
    from PIL import ImageFilter

    d = tmp_path / "e2e"
    d.mkdir()
    S = 256

    def save(name, arr, dt):
        img = Image.fromarray(arr)
        exif = Image.Exif()
        exif[0x9003] = dt
        exif[0x9004] = dt
        img.save(d / name, exif=exif)
        return str(d / name)

    def checkerboard():
        ch = (np.indices((S, S)).sum(axis=0) // 32 % 2 * 255).astype(np.uint8)
        return np.stack([ch, ch, ch], axis=-1)

    def gradient():
        x = np.tile(np.linspace(0, 255, S).astype(np.uint8), (S, 1))
        return np.stack([x, x, x], axis=-1)

    paths = {}
    # 连拍组 A：清晰棋盘 + 2 张模糊版
    cb = checkerboard()
    paths["burstA_0"] = save("burstA_0.jpg", cb, "2024:01:01 10:00:01")
    paths["burstA_1"] = save("burstA_1.jpg",
                             np.asarray(Image.fromarray(cb).filter(ImageFilter.GaussianBlur(6))),
                             "2024:01:01 10:00:01.2")
    paths["burstA_2"] = save("burstA_2.jpg",
                             np.asarray(Image.fromarray(cb).filter(ImageFilter.GaussianBlur(9))),
                             "2024:01:01 10:00:01.4")
    # 连拍组 B：两张近同渐变
    g0 = gradient()
    for k in range(2):
        a = g0.copy()
        a[10:26, 10:26] = np.full((16, 16, 3), 200, dtype=np.uint8)
        paths[f"burstB_{k}"] = save(f"burstB_{k}.jpg", a,
                                    f"2024:01:01 10:00:2{k}")
    # 单张：模糊（纯色灰）
    paths["blurry"] = save("blurry.jpg", np.full((S, S, 3), 128, dtype=np.uint8),
                           "2024:01:01 10:00:40")
    # 单张：过曝（纯白）
    paths["overexposed"] = save("overexposed.jpg", np.full((S, S, 3), 255, dtype=np.uint8),
                                "2024:01:01 10:00:50")
    # 单张：清晰（横条纹）
    rows = np.zeros((S, S), dtype=np.uint8)
    for i in range(0, S, 32):
        rows[i:i + 16] = 255
    stripes = np.stack([rows, rows, rows], axis=-1)
    paths["sharp"] = save("sharp.jpg", stripes, "2024:01:01 10:01:00")
    return d, paths
