"""Track duplication for Ableton .als files, replicating Ableton's own Cmd-D.

ID model (verified empirically against Ableton 12.4.2's own duplicates)
----------------------------------------------------------------------
Ableton uses two id spaces inside a Live Set:

* **Pointee space** — a single global counter stored in ``<NextPointeeId>``.
  Every *definition* in this space carries an ``Id`` attribute and lives on one
  of these element kinds:
    - ``AutomationTarget``, ``ModulationTarget``, ``Pointee``
    - anything ending in ``ModulationTarget`` (``VolumeModulationTarget`` …)
    - ``ControllerTargets.N`` (macro-control targets)
  The *only* thing that references a definition is ``<PointeeId Value="…">``
  (automation/clip-envelope targets). Nothing else does — other numeric fields
  that happen to equal an id are coincidence, never references.

* **Local spaces** — track ids, device ids, clip/slot ids, parameter indices,
  warp markers, etc. These repeat across the project (not globally unique) and
  Ableton leaves them untouched when duplicating.

So duplicating a track == deep-copy, then:
  1. give every pointee *definition* in the copy a fresh id from NextPointeeId
     (sequential, in document order — exactly what Ableton does),
  2. rewrite every ``PointeeId`` that pointed at one of those definitions,
  3. give the track a fresh track id,
  4. bump ``NextPointeeId``.
Local ids are deliberately left alone.
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from typing import Optional

TRACK_TAGS = ("AudioTrack", "MidiTrack", "GroupTrack", "ReturnTrack")


def is_pointee_def(tag: str) -> bool:
    """True if an element of this tag owns a pointee-space id (a definition)."""
    return (
        tag in ("AutomationTarget", "ModulationTarget", "Pointee")
        or tag.endswith("ModulationTarget")
        or tag.startswith("ControllerTargets")
    )


def get_next_pointee_id(root: ET.Element) -> int:
    e = root.find(".//NextPointeeId")
    if e is None:
        raise LookupError("no <NextPointeeId> in document")
    return int(e.get("Value"))


def set_next_pointee_id(root: ET.Element, value: int) -> None:
    root.find(".//NextPointeeId").set("Value", str(value))


def max_track_id(root: ET.Element) -> int:
    ids = [int(t.get("Id")) for t in root.iter()
           if t.tag in TRACK_TAGS and t.get("Id") and t.get("Id").lstrip("-").isdigit()]
    return max(ids) if ids else 0


def tracks_parent(root: ET.Element) -> ET.Element:
    p = root.find(".//LiveSet/Tracks")
    if p is None:
        raise LookupError("no <LiveSet><Tracks> in document")
    return p


def _set_name(track: ET.Element, name: str) -> None:
    n = track.find(".//Name")
    if n is None:
        return
    for tag in ("EffectiveName", "UserName"):
        e = n.find(tag)
        if e is not None:
            e.set("Value", name if tag == "EffectiveName" else name)


def _mute(track: ET.Element) -> None:
    """Turn the track activator (speaker) off, like the user's COMPAT workflow."""
    spk = track.find("./DeviceChain/Mixer/Speaker/Manual")
    if spk is not None:
        spk.set("Value", "false")


def remap_pointee_ids(root: ET.Element, subtree: ET.Element) -> dict:
    """Give every pointee *definition* in *subtree* a fresh id and fix internal
    ``PointeeId`` references. Bumps ``NextPointeeId``. Returns old->new map.

    Use this whenever a subtree carrying pointee ids from one context is spliced
    into a document (track duplication, grafting a harvested device template).
    """
    next_id = get_next_pointee_id(root)
    idmap: dict = {}
    for x in subtree.iter():
        if "Id" in x.attrib and is_pointee_def(x.tag):
            idmap[x.attrib["Id"]] = str(next_id)
            x.set("Id", str(next_id))
            next_id += 1
    for x in subtree.iter():
        if x.tag == "PointeeId" and x.get("Value") in idmap:
            x.set("Value", idmap[x.get("Value")])
    set_next_pointee_id(root, next_id)
    return idmap


def duplicate_track(root: ET.Element, track: ET.Element,
                    new_name: Optional[str] = None, mute: bool = True) -> ET.Element:
    """Insert a duplicate of *track* right after it and return the new element.

    Mutates ``root`` (inserts the track, bumps NextPointeeId).
    """
    dup = copy.deepcopy(track)

    # fresh pointee ids + reference fixups (mirrors Ableton's Cmd-D)
    remap_pointee_ids(root, dup)

    # fresh track id
    dup.set("Id", str(max_track_id(root) + 1))

    if new_name:
        _set_name(dup, new_name)
    if mute:
        _mute(dup)

    # 5. splice in right after the original
    parent = tracks_parent(root)
    idx = list(parent).index(track)
    dup.tail = track.tail
    parent.insert(idx + 1, dup)
    return dup


def validate_duplicate(root: ET.Element, dup: ET.Element) -> list:
    """Return a list of problems (empty == clean). Defensive safety net."""
    problems = []

    # a) every PointeeId in the dup must resolve to a definition somewhere
    all_defs = {x.get("Id") for x in root.iter()
                if "Id" in x.attrib and is_pointee_def(x.tag)}
    for x in dup.iter():
        if x.tag == "PointeeId" and x.get("Value") not in all_defs and x.get("Value") != "0":
            problems.append(f"dangling PointeeId {x.get('Value')}")

    # b) the dup's new pointee ids must be unique project-wide
    dup_def_ids = [x.get("Id") for x in dup.iter()
                   if "Id" in x.attrib and is_pointee_def(x.tag)]
    seen = {}
    for t in root.iter():
        if "Id" in t.attrib and is_pointee_def(t.tag):
            seen[t.get("Id")] = seen.get(t.get("Id"), 0) + 1
    for did in dup_def_ids:
        if seen.get(did, 0) > 1:
            problems.append(f"duplicate pointee id {did}")

    # c) track id must be unique
    tid = dup.get("Id")
    same = [t for t in root.iter() if t.tag in TRACK_TAGS and t.get("Id") == tid]
    if len(same) != 1:
        problems.append(f"track id {tid} not unique ({len(same)})")

    return sorted(set(problems))
