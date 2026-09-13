from __future__ import annotations

from engine.store import PhotoStore
from services.review_service import ReviewFilter, ReviewService, ViewMode


def _store(tmp_path):
    store = PhotoStore(str(tmp_path / "review.db"), enable_wal=False)
    rows = [
        ("/photos/a.CR3", "pair-a", "raw", 1, 1, 0, 0, 0, 4, None),
        ("/photos/a.JPG", "pair-a", "jpeg", 1, 1, 0, 0, 0, 4, None),
        ("/photos/b.CR3", "pair-b", "raw", 1, 0, 1, 0, 0, 0, None),
        ("/photos/c.CR3", "pair-c", "raw", None, 0, 0, 1, 1, 0, "X"),
        ("/photos/d.CR3", "pair-d", "raw", None, 0, 0, 0, 0, 5, "P"),
        ("/photos/e.CR3", "pair-e", "raw", None, 0, 0, 0, 0, 3, None),
    ]
    for path, asset, role, group, best, uncertain, waste, loser, star, label in rows:
        store.upsert_photo(
            {
                "path": path,
                "fname": path.rsplit("/", 1)[-1],
                "asset_pair_id": asset,
                "asset_role": role,
                "group_id": group,
                "is_best": best,
                "is_uncertain": uncertain,
                "is_waste": waste,
                "is_similar_loser": loser,
                "star": star,
                "label": label,
                "decision_source": "manual" if label else None,
                "candidate_rank": {"a.CR3": 1, "b.CR3": 2}.get(path.rsplit("/", 1)[-1]),
                "is_candidate": int(group is not None),
                "blur_score": 12.0 if asset == "pair-e" else 88.0,
            }
        )
    store.set_group(1, 2, "/photos/a.CR3", is_uncertain=True, n_candidates=2)
    return store


def test_review_snapshot_is_single_bulk_read_and_assets_are_paired(tmp_path):
    store = _store(tmp_path)
    try:
        snapshot = store.review_snapshot()
        assert len(snapshot["photos"]) == 6
        assert len(snapshot["groups"]) == 1
        assert "face_regions" in snapshot
        service = ReviewService(store)
        assert len(service.items) == 5
        assert len(service.items["pair-a"].paths) == 2
        assert service.items["pair-a"].preview_path == "/photos/a.JPG"
    finally:
        store.close()


def test_default_view_mode_and_user_tab_preference_persist(tmp_path):
    store = _store(tmp_path)
    try:
        service = ReviewService(store)
        service.focus("pair-d")
        assert service.view_mode == ViewMode.SINGLE
        service.focus("pair-a")
        assert service.view_mode == ViewMode.COMPARE

        assert service.toggle_view_mode() == ViewMode.SINGLE
        assert store.get_preference("review.view_mode") == "single"
        reopened = ReviewService(store)
        reopened.focus("pair-a")
        assert reopened.view_mode == ViewMode.SINGLE
    finally:
        store.close()


def test_filters_return_asset_counts_and_keep_nearest_cursor(tmp_path):
    store = _store(tmp_path)
    try:
        service = ReviewService(store)
        assert service.filter_counts() == {
            ReviewFilter.ALL: 5,
            ReviewFilter.AI_PICKS: 2,
            ReviewFilter.UNCERTAIN: 1,
            ReviewFilter.REJECTED: 1,
            ReviewFilter.TECHNICAL_ISSUE: 1,
            ReviewFilter.STAR: 2,
        }
        service.focus("pair-d")
        service.set_filter(ReviewFilter.STAR, star=5)
        assert service.current.asset_id == "pair-d"
        service.set_filter(ReviewFilter.UNCERTAIN)
        assert service.current.asset_id == "pair-b"
        assert [item.asset_id for item in service.queue] == ["pair-b"]
    finally:
        store.close()


def test_technical_filter_uses_analysis_threshold(tmp_path):
    store = _store(tmp_path)
    store.update_photo("/photos/e.CR3", blur_score=40.0)
    try:
        service = ReviewService(store)
        service.set_filter(ReviewFilter.TECHNICAL_ISSUE)
        assert [item.asset_id for item in service.queue] == ["pair-e"]
    finally:
        store.close()


def test_pick_and_star_decisions_sync_both_members(tmp_path):
    store = _store(tmp_path)
    try:
        service = ReviewService(store)
        service.focus("pair-a")
        service.decide("P")
        for path in ("/photos/a.CR3", "/photos/a.JPG"):
            row = store.get_photo(path)
            assert (row["star"], row["label"], row["decision_source"]) == (
                5,
                "P",
                "manual",
            )
        service.set_star(2)
        for path in ("/photos/a.CR3", "/photos/a.JPG"):
            assert store.get_photo(path)["star"] == 2
        service.decide("X")
        for path in ("/photos/a.CR3", "/photos/a.JPG"):
            row = store.get_photo(path)
            assert (row["star"], row["label"]) == (0, "X")
    finally:
        store.close()


def test_candidate_letters_and_navigation_boundaries(tmp_path):
    store = _store(tmp_path)
    try:
        service = ReviewService(store)
        service.focus("pair-a")
        assert service.select_candidate("B").asset_id == "pair-b"
        assert service.select_candidate("A").asset_id == "pair-a"
        assert service.select_candidate("D") is None
        service.set_filter(ReviewFilter.UNCERTAIN)
        assert service.previous().asset_id == "pair-b"
        assert service.next().asset_id == "pair-b"
    finally:
        store.close()


def test_undo_restores_every_member_and_each_consecutive_operation(tmp_path):
    store = _store(tmp_path)
    try:
        service = ReviewService(store)
        service.focus("pair-a")
        service.decide("P")
        service.set_star(3)
        service.decide("X")

        assert service.undo() is True
        for path in ("/photos/a.CR3", "/photos/a.JPG"):
            assert (store.get_photo(path)["star"], store.get_photo(path)["label"]) == (3, "P")
        assert service.undo() is True
        for path in ("/photos/a.CR3", "/photos/a.JPG"):
            assert (store.get_photo(path)["star"], store.get_photo(path)["label"]) == (5, "P")
        assert service.undo() is True
        for path in ("/photos/a.CR3", "/photos/a.JPG"):
            row = store.get_photo(path)
            assert (row["star"], row["label"], row["decision_source"]) == (4, None, None)
        assert service.undo() is False
    finally:
        store.close()
