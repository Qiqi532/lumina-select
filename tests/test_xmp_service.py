from __future__ import annotations

import shutil
from pathlib import Path

from lxml import etree

from services.xmp_service import LUMINA_NS, XmpSelection, write_sidecar


FIXTURE = Path(__file__).parent / "fixtures" / "xmp" / "lightroom-existing.xmp"
NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "crs": "http://ns.adobe.com/camera-raw-settings/1.0/",
    "mask": "http://ns.adobe.com/camera-raw-settings/1.0/mask/",
    "vendor": "https://example.invalid/vendor/1.0/",
    "lumina": LUMINA_NS,
}


def _selection(**overrides) -> XmpSelection:
    values = {
        "rating": 5,
        "label": "Green",
        "keywords": ("family", "精选"),
        "app_version": "0.5.0",
        "backend": "heuristic",
        "reasons": ("best-in-group", "eyes-open"),
    }
    values.update(overrides)
    return XmpSelection(**values)


def _parse(path: Path):
    return etree.parse(str(path), etree.XMLParser(resolve_entities=False, no_network=True))


def test_creates_valid_raw_sidecar_with_managed_fields(tmp_path):
    sidecar = tmp_path / "IMG_0001.xmp"

    result = write_sidecar(sidecar, _selection())

    assert result.success is True
    assert result.created is True
    assert result.backup_path is None
    tree = _parse(sidecar)
    description = tree.find(".//rdf:Description", NS)
    assert description.get(f"{{{NS['xmp']}}}Rating") == "5"
    assert description.get(f"{{{NS['xmp']}}}Label") == "Green"
    assert description.get(f"{{{LUMINA_NS}}}AppVersion") == "0.5.0"
    assert description.get(f"{{{LUMINA_NS}}}Backend") == "heuristic"
    assert tree.xpath("//dc:subject/rdf:Bag/rdf:li/text()", namespaces=NS) == [
        "family",
        "精选",
    ]
    assert tree.xpath("//lumina:Reasons/rdf:Bag/rdf:li/text()", namespaces=NS) == [
        "best-in-group",
        "eyes-open",
    ]


def test_merges_without_losing_lightroom_or_unknown_metadata(tmp_path):
    sidecar = tmp_path / "IMG_0002.xmp"
    shutil.copy2(FIXTURE, sidecar)
    before = _parse(sidecar)
    before_mask_name = before.xpath(
        "string(//crs:MaskGroupBasedCorrections/rdf:Seq/rdf:li/mask:Name)",
        namespaces=NS,
    )
    before_mask_kind = before.xpath(
        "string(//crs:MaskGroupBasedCorrections/rdf:Seq/rdf:li/mask:What)",
        namespaces=NS,
    )
    before_unknown = before.find(".//vendor:UnknownNode", NS)

    result = write_sidecar(sidecar, _selection())

    assert result.success is True
    tree = _parse(sidecar)
    description = tree.find(".//rdf:Description", NS)
    assert description.get(f"{{{NS['crs']}}}Exposure2012") == "0.35"
    assert description.get(f"{{{NS['crs']}}}CropTop") == "0.125"
    assert description.get(f"{{{NS['vendor']}}}OpaqueSetting") == "keep-me"
    assert tree.xpath(
        "string(//crs:MaskGroupBasedCorrections/rdf:Seq/rdf:li/mask:Name)",
        namespaces=NS,
    ) == before_mask_name
    assert tree.xpath(
        "string(//crs:MaskGroupBasedCorrections/rdf:Seq/rdf:li/mask:What)",
        namespaces=NS,
    ) == before_mask_kind
    unknown = tree.find(".//vendor:UnknownNode", NS)
    assert unknown.text == before_unknown.text
    assert unknown.get(f"{{{NS['vendor']}}}key") == before_unknown.get(
        f"{{{NS['vendor']}}}key"
    )
    assert tree.xpath("//dc:subject/rdf:Bag/rdf:li/text()", namespaces=NS) == [
        "旅行",
        "family",
        "精选",
    ]


def test_in_place_update_backs_up_existing_sidecar_first(tmp_path):
    sidecar = tmp_path / "IMG_0003.xmp"
    original = FIXTURE.read_bytes()
    sidecar.write_bytes(original)
    backup_root = tmp_path / "backups"

    result = write_sidecar(
        sidecar,
        _selection(rating=4),
        in_place=True,
        project_uuid="project-123",
        backup_root=backup_root,
    )

    assert result.success is True
    assert result.backup_path is not None
    assert result.backup_path.read_bytes() == original
    assert result.backup_path.parent == backup_root / "project-123"


def test_atomic_write_failure_keeps_original_bytes(tmp_path, monkeypatch):
    from services import xmp_service

    sidecar = tmp_path / "IMG_0004.xmp"
    original = FIXTURE.read_bytes()
    sidecar.write_bytes(original)

    def deny_replace(_source, _target):
        raise PermissionError("read only")

    monkeypatch.setattr(xmp_service.os, "replace", deny_replace)

    result = write_sidecar(sidecar, _selection())

    assert result.success is False
    assert result.error_code == "permission_denied"
    assert sidecar.read_bytes() == original
    assert list(tmp_path.glob("*.lumina-part")) == []


def test_invalid_xml_returns_structured_error_without_overwrite(tmp_path):
    sidecar = tmp_path / "IMG_0005.xmp"
    original = b"<not-valid>"
    sidecar.write_bytes(original)

    result = write_sidecar(sidecar, _selection())

    assert result.success is False
    assert result.error_code == "xmp_invalid"
    assert result.message
    assert sidecar.read_bytes() == original
