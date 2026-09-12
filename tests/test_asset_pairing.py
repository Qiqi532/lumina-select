from __future__ import annotations

from pathlib import Path

import pytest

from services.asset_pairing import pair_assets


def _pair(tmp_path, names, timestamps):
    paths = []
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        paths.append(path)
    return pair_assets(paths, lambda path: timestamps.get(path.name))


def test_same_directory_stem_and_close_time_form_one_asset(tmp_path):
    assets = _pair(
        tmp_path,
        ["IMG_0001.CR3", "IMG_0001.JPG"],
        {"IMG_0001.CR3": 1_000.0, "IMG_0001.JPG": 2_500.0},
    )

    assert len(assets) == 1
    assert assets[0].raw_path.name == "IMG_0001.CR3"
    assert assets[0].jpeg_path.name == "IMG_0001.JPG"
    assert assets[0].preview_path == assets[0].jpeg_path
    assert assets[0].members_for_mode("raw+jpeg") == (
        (assets[0].raw_path, assets[0].jpeg_path),
        None,
    )


def test_isolated_raw_and_jpeg_remain_separate(tmp_path):
    assets = _pair(
        tmp_path,
        ["RAW_ONLY.NEF", "JPEG_ONLY.jpg"],
        {"RAW_ONLY.NEF": 1_000.0, "JPEG_ONLY.jpg": 1_000.0},
    )
    assert len(assets) == 2
    assert sum(asset.raw_path is not None for asset in assets) == 1
    assert sum(asset.jpeg_path is not None for asset in assets) == 1


@pytest.mark.parametrize(
    ("names", "timestamps"),
    [
        (
            ["IMG_0001.CR3", "IMG_0001.JPG"],
            {"IMG_0001.CR3": 1_000.0, "IMG_0001.JPG": 31_000.0},
        ),
        (
            ["a/IMG_0001.CR3", "b/IMG_0001.JPG"],
            {"IMG_0001.CR3": 1_000.0, "IMG_0001.JPG": 1_500.0},
        ),
    ],
)
def test_distant_time_or_directory_does_not_pair(tmp_path, names, timestamps):
    assert len(_pair(tmp_path, names, timestamps)) == 2


def test_extension_case_is_ignored_and_asset_id_is_stable(tmp_path):
    names = ["IMG_0001.cR3", "IMG_0001.JpG"]
    timestamps = {name: 1_000.0 for name in names}
    first = _pair(tmp_path, names, timestamps)
    second = _pair(tmp_path, reversed(names), timestamps)
    assert len(first) == len(second) == 1
    assert first[0].asset_id == second[0].asset_id


def test_one_raw_chooses_nearest_jpeg_and_leaves_other_independent(tmp_path):
    assets = _pair(
        tmp_path,
        ["IMG_0001.CR3", "IMG_0001.JPG", "IMG_0001.JPEG"],
        {
            "IMG_0001.CR3": 10_000.0,
            "IMG_0001.JPG": 10_800.0,
            "IMG_0001.JPEG": 11_900.0,
        },
    )
    assert len(assets) == 2
    paired = next(asset for asset in assets if asset.raw_path is not None)
    assert paired.jpeg_path.name == "IMG_0001.JPG"
    assert any(
        asset.raw_path is None and asset.jpeg_path.name == "IMG_0001.JPEG"
        for asset in assets
    )


def test_missing_requested_member_returns_reason(tmp_path):
    raw = _pair(tmp_path, ["ONLY.CR3"], {"ONLY.CR3": 1_000.0})[0]
    assert raw.members_for_mode("jpeg") == ((), "jpeg_not_available")
    assert raw.members_for_mode("raw") == ((raw.raw_path,), None)
    with pytest.raises(ValueError, match="asset mode"):
        raw.members_for_mode("invalid")
