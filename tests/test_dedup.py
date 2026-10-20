from engine.pipeline import _same_asset


def test_raw_and_jpeg_members_are_not_duplicate_candidates():
    raw = {"path": "/a/IMG_1.CR3", "asset_pair_id": "asset-1"}
    jpeg = {"path": "/a/IMG_1.JPG", "asset_pair_id": "asset-1"}
    other = {"path": "/a/IMG_2.JPG", "asset_pair_id": "asset-2"}

    assert _same_asset(raw, jpeg) is True
    assert _same_asset(raw, other) is False
