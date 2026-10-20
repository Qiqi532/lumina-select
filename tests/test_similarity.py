# -*- coding: utf-8 -*-
"""similarity.py 单测：连拍分组、pHash 相似、并查集聚类合并。

说明：group_similar 内部会调用 load_image(path) 然后 phash_of_image(img)。
这里把 load_image 伪装成返回 path 本身，再让 phash_of_image 按 path 返回
预置哈希，从而在不读真实图片的前提下覆盖纯聚类逻辑。
"""
from __future__ import annotations

import numpy as np

from engine.similarity import (
    UnionFind,
    _SWAR_CHUNK_ROWS,
    _hamming_hex,
    _popcount,
    _popcount_swar,
    group_similar,
)

H0 = "0" * 16
H1 = "3000000000000000"   # 与 H0 汉明距离 2
H2 = "7000000000000000"   # 与 H1 距离 2、与 H0 距离 4
HF = "f" * 16             # 与 H0 距离 64
H55 = "5" * 16            # 与 H0/HF 各距离 32（互相远离）


def test_union_find_basic():
    uf = UnionFind(5)
    uf.union(0, 1)
    uf.union(3, 4)
    assert uf.find(0) == uf.find(1)
    assert uf.find(0) != uf.find(2)
    roots = uf.roots()
    assert len(roots) == 3  # {0,1} {2} {3,4}


def test_union_find_path_compression():
    uf = UnionFind(4)
    uf.union(0, 1)
    uf.union(2, 3)
    uf.union(1, 2)   # 合并两个组
    assert len(uf.roots()) == 1


def test_hamming_hex():
    assert _hamming_hex("0" * 16, "0" * 16) == 0
    # 翻转所有位 -> 64
    assert _hamming_hex("0" * 16, "f" * 16) == 64
    assert _hamming_hex("", "abc") == 255


def _setup(monkeypatch, phash_map):
    monkeypatch.setattr("engine.similarity.load_image", lambda p, **kw: p)
    monkeypatch.setattr("engine.similarity.phash_of_image", lambda img: phash_map[img])


def test_group_similar_burst_only(monkeypatch):
    """时间戳相邻（间隔 < 阈值）应聚为一组，且不触发 pHash 合并（哈希全不同）。"""
    paths = ["a.jpg", "b.jpg", "c.jpg"]
    ts = [0.0, 1000.0, 5000.0]   # a-b 连拍；c 独立
    _setup(monkeypatch, {"a.jpg": H0, "b.jpg": HF, "c.jpg": H55})
    _, gids, groups = group_similar(paths, ts)
    assert gids[0] == gids[1] != gids[2]
    assert len(groups) == 2


def test_group_similar_burst_threshold(monkeypatch):
    """间隔恰好等于阈值（1500ms）不算连拍。"""
    paths = ["a.jpg", "b.jpg"]
    ts = [0.0, 1500.0]
    _setup(monkeypatch, {"a.jpg": H0, "b.jpg": HF})
    _, gids, groups = group_similar(paths, ts)
    assert gids[0] != gids[1]
    assert len(groups) == 2


def test_group_similar_phash_merge(monkeypatch):
    """pHash 相近的跨时间照片应被并查集合并进同一组。"""
    paths = ["x.jpg", "y.jpg", "z.jpg"]
    ts = [1000.0, 200000.0, 300000.0]  # 时间相差很大，仅靠 pHash
    _setup(monkeypatch, {"x.jpg": H0, "y.jpg": H1, "z.jpg": HF})
    _, gids, groups = group_similar(paths, ts)
    assert gids[0] == gids[1] != gids[2]
    assert len(groups) == 2


def test_group_similar_chain_merge(monkeypatch):
    """A-B 连拍、B-C 相似 → 三者应合并为一组（传递合并）。"""
    paths = ["a.jpg", "b.jpg", "c.jpg"]
    ts = [0.0, 1000.0, 500000.0]     # a-b 连拍
    _setup(monkeypatch, {"a.jpg": H0, "b.jpg": H1, "c.jpg": H2})
    _, gids, groups = group_similar(paths, ts)
    assert gids[0] == gids[1] == gids[2]
    assert len(groups) == 1


def test_group_similar_no_ts_no_hash_single(monkeypatch):
    """无时间戳且无有效哈希时，每张独立成组。"""
    paths = ["a.jpg", "b.jpg"]
    ts = [None, None]
    monkeypatch.setattr("engine.similarity.load_image", lambda p, **kw: p)
    monkeypatch.setattr("engine.similarity.phash_of_image", lambda img: None)
    _, gids, groups = group_similar(paths, ts)
    assert gids[0] != gids[1]
    assert len(groups) == 2


# ---------------------------------------------------------------------------
# v0.6 S3-2 / P1-4：位计数不得退回 unpackbits（那会把内存一次性放大 64 倍）
# ---------------------------------------------------------------------------

def _random_u64(rng, shape):
    raw = rng.integers(0, 256, size=shape + (8,), dtype=np.uint8)
    return raw.view(np.uint64).reshape(shape)


def _reference_popcount(x):
    """参考实现（正确但内存放大 64×），仅用于小数组对照。

    注意每个 uint64 = 8 字节 = 64 位，展开后必须 reshape 回 (…, 64) 再按最后
    一维求和，否则会退化成「整行位总数」而不是「每元素置位数」。
    """
    x = np.asarray(x, dtype=np.uint64)
    bits = np.unpackbits(x.view(np.uint8), axis=-1)
    return bits.reshape(x.shape + (64,)).sum(axis=-1).astype(np.uint8)


def test_popcount_matches_reference_on_random_data():
    rng = np.random.default_rng(20261011)
    x = _random_u64(rng, (7, 33))
    expected = _reference_popcount(x)
    assert np.array_equal(_popcount(x), expected)
    assert np.array_equal(_popcount_swar(x), expected)


def test_popcount_handles_edge_values():
    x = np.array([0, 1, 0xFFFFFFFFFFFFFFFF, 0x8000000000000000], dtype=np.uint64)
    assert _popcount(x).tolist() == [0, 1, 64, 1]
    assert _popcount_swar(x).tolist() == [0, 1, 64, 1]


def test_popcount_never_falls_back_to_unpackbits(monkeypatch):
    """回归守卫：实现不得再退回 np.unpackbits。"""
    def boom(*args, **kwargs):  # pragma: no cover - 触发即失败
        raise AssertionError("_popcount 不应使用 np.unpackbits（内存放大 64×）")

    monkeypatch.setattr(np, "unpackbits", boom)
    x = np.array([[0, 3, 7, 255, 0xFFFF]], dtype=np.uint64)
    assert _popcount(x).tolist() == [[0, 2, 3, 8, 16]]


def test_popcount_swar_correct_across_multiple_chunks():
    """行数跨过多个分块边界时仍需正确。"""
    rows = _SWAR_CHUNK_ROWS * 2 + 3
    rng = np.random.default_rng(7)
    x = _random_u64(rng, (rows, 5))
    assert np.array_equal(_popcount_swar(x), _reference_popcount(x))
