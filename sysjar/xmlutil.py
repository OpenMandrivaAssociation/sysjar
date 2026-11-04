"""Small XML helpers for Maven POMs and Ivy files. No namespace package required."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path


POM_NS = "http://maven.apache.org/POM/4.0.0"


def local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def qname(parent: ET.Element, tag: str) -> str:
    if parent.tag.startswith("{"):
        return parent.tag.split("}", 1)[0] + "}" + tag
    return tag


def children(elem: ET.Element | None, tag: str) -> list[ET.Element]:
    if elem is None:
        return []
    return [child for child in list(elem) if local_name(child.tag) == tag]


def child(elem: ET.Element | None, tag: str) -> ET.Element | None:
    found = children(elem, tag)
    return found[0] if found else None


def text_of(elem: ET.Element | None, tag: str) -> str:
    node = child(elem, tag)
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def upsert(parent: ET.Element, tag: str, value: str, *, after: tuple[str, ...] = ()) -> ET.Element:
    node = child(parent, tag)
    if node is None:
        node = ET.Element(qname(parent, tag))
        names = [local_name(item.tag) for item in list(parent)]
        index = len(list(parent))
        for marker in after:
            if marker in names:
                index = names.index(marker) + 1
                break
        parent.insert(index, node)
        node.tail = "\n"
    node.text = value
    return node


def parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    return {node: parent for parent in root.iter() if local_name(parent.tag) for node in list(parent)}


def remove_node(root: ET.Element, node: ET.Element) -> None:
    mapping = parent_map(root)
    parent = mapping.get(node)
    if parent is not None:
        parent.remove(node)


def parse_xml(path: Path) -> ET.ElementTree:
    return ET.parse(path)


def write_xml(tree: ET.ElementTree, path: Path) -> None:
    root = tree.getroot()
    if root.tag.startswith("{"):
        uri = root.tag.split("}", 1)[0][1:]
        ET.register_namespace("", uri)
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="utf-8", xml_declaration=True)
