from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import numpy as np
from PIL import Image

from engine import loader


def _jpeg_bytes(value=90):
    buffer = BytesIO()
    Image.fromarray(np.full((8, 10, 3), value, dtype=np.uint8)).save(
        buffer, format="JPEG"
    )
    return buffer.getvalue()


class _FakeRaw:
    def __init__(self, thumb=None):
        self.thumb = thumb
        self.postprocess_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def extract_thumb(self):
        if self.thumb is None:
            raise RuntimeError("no embedded preview")
        return SimpleNamespace(format="JPEG", data=self.thumb)

    def postprocess(self, **kwargs):
        self.postprocess_calls.append(kwargs)
        return np.full((6, 7, 3), 120, dtype=np.uint8)


def test_review_preview_prefers_embedded_raw_jpeg(monkeypatch):
    raw = _FakeRaw(_jpeg_bytes())
    monkeypatch.setattr(loader.rawpy, "imread", lambda _path: raw)

    image = loader.load_review_preview("C:/photos/IMG_0001.CR3")

    assert image.size == (10, 8)
    assert raw.postprocess_calls == []


def test_review_preview_falls_back_to_half_size_raw(monkeypatch):
    raw = _FakeRaw()
    monkeypatch.setattr(loader.rawpy, "imread", lambda _path: raw)

    image = loader.load_review_preview("C:/photos/IMG_0001.NEF")

    assert image.size == (7, 6)
    assert raw.postprocess_calls == [{"use_camera_wb": True, "half_size": True}]


def test_review_preview_uses_paired_jpeg_without_opening_raw(tmp_path, monkeypatch):
    jpeg = tmp_path / "IMG_0001.JPG"
    jpeg.write_bytes(_jpeg_bytes(180))
    monkeypatch.setattr(
        loader.rawpy,
        "imread",
        lambda _path: (_ for _ in ()).throw(AssertionError("RAW must not open")),
    )

    image = loader.load_review_preview(
        "C:/photos/IMG_0001.CR3", paired_jpeg=str(jpeg)
    )

    assert image.size == (10, 8)
