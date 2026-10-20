# -*- coding: utf-8 -*-
"""
similarity.py —— pHash 相似、连拍分组、并查集聚类

算法（设计文档 4.4.1 / 4.4.2）：
    1. 连拍分组：按 EXIF 时间戳排序，相邻间隔 < 1500ms（可配置）归入同一连拍组。
       无 EXIF 时回退为文件名相邻 + pHash 距离判组（ts 缺失时用文件顺序近似）。
    2. pHash：64 位感知哈希（imagehash.phash），汉明距离 < 12（可配置）视为相似。
    3. 并查集：把「同连拍组」或「pHash 相似」的照片合并为同一相似组，
       同时覆盖连拍组与跨机位相似组。

独立命令行调试：python -m engine.similarity <目录> [--limit N]
"""
from __future__ import annotations

import os

import numpy as np

from . import config, loader
from .loader import load_image
from .log import get_logger

try:
    import imagehash
except Exception:  # pragma: no cover
    imagehash = None

_log = get_logger("similarity")

# 阈值统一来自 engine/config.py
BURST_INTERVAL_MS = config.BURST_INTERVAL_MS     # 连拍时间间隔阈值
PHASH_THRESHOLD = config.PHASH_THRESHOLD         # pHash 汉明距离阈值
PHASH_HASH_SIZE = config.PHASH_HASH_SIZE         # 64 位 phash


class UnionFind:
    """并查集：用于把相似/连拍关系合并成组。"""

    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        # 路径压缩
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1

    def roots(self) -> dict[int, list[int]]:
        """返回 {root: [成员索引...]}，root 按组内最小索引升序。"""
        groups: dict[int, list[int]] = {}
        for i in range(len(self.parent)):
            r = self.find(i)
            groups.setdefault(r, []).append(i)
        order = sorted(groups.keys(), key=lambda r: min(groups[r]))
        return {r: groups[r] for r in order}


# ---------------------------------------------------------------------------
# pHash
# ---------------------------------------------------------------------------
def phash_of_image(img) -> str | None:
    """计算 64 位 pHash，返回十六进制字符串；失败返回 None。"""
    if imagehash is None or img is None:
        return None
    try:
        return str(imagehash.phash(img, hash_size=PHASH_HASH_SIZE))
    except Exception:
        return None


def hamming_hex(a: str, b: str) -> int:
    """两个十六进制 pHash 的汉明距离（不同位个数）。空值返回 255（视为极不相似）。"""
    if not a or not b:
        return 255
    return bin(int(a, 16) ^ int(b, 16)).count("1")


# 兼容旧私有命名的调用方
_hamming_hex = hamming_hex


def _hamming_matrix(hexes: list[str]) -> np.ndarray:
    """向量化计算两两汉明距离矩阵。

    ⚠️ 返回 n² 的矩阵，仅用于小批量（调试/单测）。生产路径请用
    iter_similar_pairs()——它是流式生成器，内存占用恒定。
    """
    n = len(hexes)
    mat = np.zeros((n, n), dtype=np.uint8)
    if n == 0:
        return mat
    vals = np.array([int(h, 16) if h else 0 for h in hexes], dtype=np.uint64)
    chunk = 128
    for i0 in range(0, n, chunk):
        i1 = min(i0 + chunk, n)
        xor = vals[i0:i1][:, None] ^ vals[None, :]          # (chunk, n) uint64
        mat[i0:i1] = _popcount(xor)
    return mat


# --- SWAR（SIMD Within A Register）并行位计数常量（Hacker's Delight §5-1） ---
_SWAR_M1 = np.uint64(0x5555555555555555)   # 0101…
_SWAR_M2 = np.uint64(0x3333333333333333)   # 0011…
_SWAR_M4 = np.uint64(0x0F0F0F0F0F0F0F0F)   # 0000_1111…
_SWAR_H01 = np.uint64(0x0101010101010101)
# 分块行数：把临时数组峰值压到 O(_SWAR_CHUNK_ROWS × n)，与照片总数无关
_SWAR_CHUNK_ROWS = 32


def _popcount(xor: np.ndarray) -> np.ndarray:
    """计算 uint64 数组中每个元素的置位数（即汉明距离）。

    实现按优先级：
      1. numpy >= 2.0 的 ``np.bitwise_count`` —— 原生位计数，无额外大数组；
      2. SWAR 并行位计数（``_popcount_swar``，按行分块）—— 峰值额外内存
         ≈ ``2 × _SWAR_CHUNK_ROWS × n × 8B``，与照片总数无关。

    【v0.6 S3-2 / P1-4】旧实现退回 ``np.unpackbits``，会把 ``(rows, n)`` 的
    uint64 展开成 ``(rows, n, 64)`` 的 uint8 —— **一次性放大 64 倍**：
    ``iter_similar_pairs`` 取 block=256、n=50000 时单次约 800 MB，与本文件声称的
    「内存占用与照片总数无关」自相矛盾。（本项目 numpy 1.26 **没有**
    ``bitwise_count``，即那正是当时实际生效的路径，不是死代码。）
    """
    fn = getattr(np, "bitwise_count", None)
    if fn is not None:
        try:
            return np.asarray(fn(xor), dtype=np.uint8)
        except Exception:
            pass
    return _popcount_swar(xor)


def _popcount_swar(xor: np.ndarray) -> np.ndarray:
    """SWAR 并行位计数，按行分块执行，避免任何与整个输入同尺寸的大临时数组。

    在一个 uint64 内部完成「2 位 → 4 位 → 8 位一组」的加法（掩码移位），
    因此额外内存只是**一块**（``_SWAR_CHUNK_ROWS × n``）量级。
    """
    xor = np.asarray(xor, dtype=np.uint64)
    out = np.empty(xor.shape, dtype=np.uint8)
    one, two, four, shift = np.uint64(1), np.uint64(2), np.uint64(4), np.uint64(56)
    rows = xor.shape[0]
    for i0 in range(0, rows, _SWAR_CHUNK_ROWS):
        i1 = min(i0 + _SWAR_CHUNK_ROWS, rows)
        x = xor[i0:i1].copy()
        t = x >> one
        t &= _SWAR_M1
        x -= t                       # 每 2 位一组内的置位数（0..2）
        t = x >> two
        t &= _SWAR_M2
        x &= _SWAR_M2
        x += t                       # 每 4 位一组（0..4）
        t = x >> four
        x += t
        x &= _SWAR_M4                # 每 8 位一组（0..8）
        x *= _SWAR_H01               # 各字节计数累加到最高字节
        x >>= shift
        out[i0:i1] = x.astype(np.uint8)
    return out


def iter_similar_pairs(hexes: list[str], threshold: int, block: int = 256):
    """流式产出所有汉明距离 < threshold 的 (i, j) 索引对（i < j）。

    【修复 v0.4】旧实现先算完整 n×n 距离矩阵，再调 np.triu_indices(m, k=1)
    生成全部上三角索引。两者的内存都是 O(n²)：
        n=10_000  →  矩阵 100MB + 索引 2×5000万×8B ≈ 800MB
        n=30_000  →  矩阵 900MB + 索引 ≈ 7.2GB  → 直接 MemoryError
    改为分块扫描：每次只算 block×n 的一小块，内存占用 O(block × n)，
    与照片总数无关。对典型相册（几百到几万张）都能稳定跑完。
    （【v0.6 S3-2】位计数本身也按行分块，见 ``_popcount_swar`` —— 块内
    不再出现 O(block × n × 64) 的 unpackbits 展开，这才让本段承诺成立。）
    """
    n = len(hexes)
    if n < 2:
        return
    vals = np.array([int(h, 16) if h else 0 for h in hexes], dtype=np.uint64)
    for i0 in range(0, n, block):
        i1 = min(i0 + block, n)
        xor = vals[i0:i1][:, None] ^ vals[None, :]        # (block, n) uint64
        dist = _popcount(xor)                             # (block, n) uint8
        # 只保留 j > i 的上三角部分，避免重复对与自比
        rows = np.arange(i0, i1)[:, None]
        cols = np.arange(n)[None, :]
        mask = (dist < threshold) & (cols > rows)
        rr, cc = np.where(mask)
        # rr 是块内行号（0 起），换算回全局索引需加 i0；cc 本身就是全局列号
        for r, c in zip(rr.tolist(), cc.tolist()):
            yield i0 + r, c



# ---------------------------------------------------------------------------
# 相似聚类主函数
# ---------------------------------------------------------------------------
def group_similar(paths: list[str], ts_list: list[float],
                  burst_interval_ms: float = BURST_INTERVAL_MS,
                  phash_threshold: int = PHASH_THRESHOLD,
                  progress_cb=None,
                  phash_hexes: list[str] | None = None) -> tuple[list[str], list[int], list[list[str]]]:
    """相似聚类主函数。

    参数：
        paths              图像绝对路径列表（与 ts_list 等长）
        ts_list            每张的拍摄时间戳（毫秒），可为 None
        burst_interval_ms  连拍间隔阈值
        phash_threshold    pHash 汉明距离阈值
        progress_cb        optional 回调(已处理数, 总数)
        phash_hexes        optional 预计算的 phash 十六进制列表（与 paths 等长；
                           由调用方传入可避免重复解码原图——增量/断点续跑场景关键优化）

    返回：
        (phash_hexes, group_ids, groups)
        phash_hexes  每张的 phash 十六进制（可为 ''）
        group_ids    每张所属相似组编号（0 起）
        groups       [[路径...], ...] 按组序
    """
    n = len(paths)
    if phash_hexes is None:
        phash_hexes = []
        for i, p in enumerate(paths):
            if progress_cb:
                progress_cb(i + 1, n)
            img = load_image(p, max_size=config.PHASH_SIZE)
            phash_hexes.append(phash_of_image(img) or "")

    uf = UnionFind(n)

    # ---- 1) 连拍分组（时间戳）----
    valid_ts = [t for t in ts_list if t is not None]
    if valid_ts:
        order = sorted(range(n), key=lambda i: (ts_list[i] if ts_list[i] is not None else 1e18))
        for k in range(len(order) - 1):
            a, b = order[k], order[k + 1]
            ta, tb = ts_list[a], ts_list[b]
            if ta is not None and tb is not None and 0 <= (tb - ta) < burst_interval_ms:
                uf.union(a, b)

    # ---- 2) pHash 相似（汉明距离 < 阈值）----
    valid_idx = [i for i, h in enumerate(phash_hexes) if h]
    m = len(valid_idx)
    if m >= 2:
        # 只对有效哈希子集做流式配对（内存 O(block × m)，与规模无关）
        sub = [phash_hexes[i] for i in valid_idx]
        for r, c in iter_similar_pairs(sub, phash_threshold):
            uf.union(valid_idx[r], valid_idx[c])

    # ---- 3) 整理组 ----
    roots = uf.roots()
    group_ids = [0] * n
    groups: list[list[str]] = []
    for gid, members in enumerate(roots.values()):
        for idx in members:
            group_ids[idx] = gid
        groups.append([paths[idx] for idx in members])
    return phash_hexes, group_ids, groups


# ---------------------------------------------------------------------------
# 命令行调试入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    import time

    root = sys.argv[1] if len(sys.argv) > 1 else "."
    paths = loader.scan_directory(root)
    ts_list = [loader.read_exif(p)["ts"] for p in paths]
    t0 = time.time()
    hexes, gids, groups = group_similar(paths, ts_list)
    print(f"共 {len(paths)} 张 -> 相似组 {len(groups)} 个（耗时 {time.time() - t0:.1f}s）")
    for gi, g in enumerate(groups):
        if len(g) > 1:
            print(f"  组{gi}: {len(g)} 张 -> {[os.path.basename(x) for x in g]}")
