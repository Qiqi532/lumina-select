"""Atomic, non-destructive Lightroom XMP sidecar updates."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tempfile
import time

from lxml import etree


XMPMETA_NS = "adobe:ns:meta/"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XMP_NS = "http://ns.adobe.com/xap/1.0/"
DC_NS = "http://purl.org/dc/elements/1.1/"
LUMINA_NS = "https://lumina-select.local/ns/1.0/"


@dataclass(frozen=True, slots=True)
class XmpSelection:
    rating: int
    label: str | None
    keywords: tuple[str, ...]
    app_version: str
    backend: str
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 0 <= self.rating <= 5:
            raise ValueError("rating must be between 0 and 5")


@dataclass(frozen=True, slots=True)
class XmpWriteResult:
    sidecar_path: Path
    backup_path: Path | None
    created: bool
    success: bool = True
    error_code: str | None = None
    message: str | None = None


def _parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, recover=False)


def _new_document() -> etree._ElementTree:
    root = etree.Element(
        etree.QName(XMPMETA_NS, "xmpmeta"),
        nsmap={
            "x": XMPMETA_NS,
            "rdf": RDF_NS,
            "xmp": XMP_NS,
            "dc": DC_NS,
            "lumina": LUMINA_NS,
        },
    )
    rdf = etree.SubElement(root, etree.QName(RDF_NS, "RDF"))
    description = etree.SubElement(rdf, etree.QName(RDF_NS, "Description"))
    description.set(etree.QName(RDF_NS, "about"), "")
    return etree.ElementTree(root)


def _description(tree: etree._ElementTree) -> etree._Element:
    nodes = tree.xpath("//rdf:Description", namespaces={"rdf": RDF_NS})
    if not nodes:
        raise etree.XMLSyntaxError("XMP has no rdf:Description", 0, 0, 0)
    return nodes[0]


def _unique(values: tuple[str, ...] | list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        value = str(raw_value).strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _set_bag(parent: etree._Element, name: etree.QName, values: list[str]) -> None:
    existing = parent.find(str(name))
    if existing is not None:
        parent.remove(existing)
    container = etree.SubElement(parent, name)
    bag = etree.SubElement(container, etree.QName(RDF_NS, "Bag"))
    for value in values:
        item = etree.SubElement(bag, etree.QName(RDF_NS, "li"))
        item.text = value


def _apply_selection(tree: etree._ElementTree, selection: XmpSelection) -> None:
    description = _description(tree)
    description.set(etree.QName(XMP_NS, "Rating"), str(selection.rating))
    if selection.label is None:
        description.attrib.pop(str(etree.QName(XMP_NS, "Label")), None)
    else:
        description.set(etree.QName(XMP_NS, "Label"), selection.label)
    description.set(etree.QName(LUMINA_NS, "AppVersion"), selection.app_version)
    description.set(etree.QName(LUMINA_NS, "Backend"), selection.backend)

    current_keywords = tree.xpath(
        "//dc:subject/rdf:Bag/rdf:li/text()",
        namespaces={"dc": DC_NS, "rdf": RDF_NS},
    )
    _set_bag(
        description,
        etree.QName(DC_NS, "subject"),
        _unique([*current_keywords, *selection.keywords]),
    )
    _set_bag(
        description,
        etree.QName(LUMINA_NS, "Reasons"),
        _unique(selection.reasons),
    )


def _default_backup_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise OSError("LOCALAPPDATA is unavailable")
    return Path(local_app_data) / "Lumina Select" / "XmpBackups"


def _backup_existing(sidecar_path: Path, backup_root: Path, project_uuid: str) -> Path:
    project_dir = backup_root / project_uuid
    project_dir.mkdir(parents=True, exist_ok=True)
    backup_path = project_dir / sidecar_path.name
    if backup_path.exists():
        backup_path = project_dir / (
            f"{sidecar_path.stem}-{time.time_ns()}{sidecar_path.suffix}"
        )
    shutil.copy2(sidecar_path, backup_path)
    with backup_path.open("ab") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    return backup_path


def _error_code(error: BaseException) -> str:
    if isinstance(error, (PermissionError, OSError)):
        return "permission_denied"
    return "unknown"


def write_sidecar(
    sidecar_path: str | Path,
    selection: XmpSelection,
    *,
    in_place: bool = False,
    project_uuid: str | None = None,
    backup_root: str | Path | None = None,
) -> XmpWriteResult:
    """Create or merge one sidecar without exposing a partially written file."""
    path = Path(sidecar_path)
    created = not path.exists()
    backup_path: Path | None = None
    temporary_path: Path | None = None

    try:
        if created:
            tree = _new_document()
        else:
            tree = etree.parse(str(path), _parser())
        _apply_selection(tree, selection)
    except (etree.XMLSyntaxError, ValueError) as error:
        return XmpWriteResult(
            path, None, created, False, "xmp_invalid", str(error)
        )

    try:
        if in_place and not created:
            if not project_uuid:
                raise ValueError("project_uuid is required for in-place XMP updates")
            root = Path(backup_root) if backup_root is not None else _default_backup_root()
            backup_path = _backup_existing(path, root, project_uuid)

        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".lumina-part", dir=path.parent
        )
        temporary_path = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            tree.write(handle, encoding="UTF-8", xml_declaration=True, pretty_print=True)
            handle.flush()
            os.fsync(handle.fileno())
        etree.parse(str(temporary_path), _parser())
        os.replace(temporary_path, path)
        temporary_path = None
        return XmpWriteResult(path, backup_path, created)
    except (ValueError, OSError) as error:
        return XmpWriteResult(
            path,
            backup_path,
            created,
            False,
            _error_code(error),
            str(error),
        )
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
