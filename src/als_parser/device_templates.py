"""Find installed plugin replacements and harvest device-node templates.

To build a working VST3/AU device for a plugin we don't synthesise the node from
scratch (we'd have to know the exact Uid / AU component ids / parameter layout).
Instead we *harvest* a real device node of that plugin from somewhere it already
exists — another project in the user's library, or the current project — and
reuse it as a scaffold. The harvested node already carries the correct identity;
the caller remaps its ids and overwrites its state.
"""

from __future__ import annotations

import copy
import gzip
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

# Where macOS keeps installed plugins.
VST3_DIRS = [
    Path("/Library/Audio/Plug-Ins/VST3"),
    Path.home() / "Library/Audio/Plug-Ins/VST3",
]
AU_DIRS = [
    Path("/Library/Audio/Plug-Ins/Components"),
    Path.home() / "Library/Audio/Plug-Ins/Components",
]


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def installed_formats(name: str) -> dict:
    """Return {"VST3": path, "AU": path} for whichever formats are installed
    for *name* (matched on the plugin file's stem, exact then normalised)."""
    found = {}
    for fmt, dirs, ext in (("VST3", VST3_DIRS, ".vst3"), ("AU", AU_DIRS, ".component")):
        for d in dirs:
            if not d.is_dir():
                continue
            for p in d.iterdir():
                if p.suffix != ext:
                    continue
                if p.stem == name or _norm(p.stem) == _norm(name):
                    found[fmt] = str(p)
                    break
            if fmt in found:
                break
    return found


# --- device-node lookup inside an .als ------------------------------------- #

def _device_format(dev: ET.Element) -> Optional[str]:
    if dev.find(".//VstPluginInfo") is not None:
        return "VST2"
    if dev.find(".//Vst3PluginInfo") is not None:
        return "VST3"
    if dev.find(".//AuPluginInfo") is not None:
        return "AU"
    return None


def _device_name(dev: ET.Element) -> Optional[str]:
    for tag in ("Vst3PluginInfo/Name", "AuPluginInfo/Name", "VstPluginInfo/PlugName"):
        e = dev.find(".//" + tag)
        if e is not None and e.get("Value"):
            return e.get("Value")
    return None


def find_device_node(root: ET.Element, name: str, fmt: str) -> Optional[ET.Element]:
    """First device node in *root* matching plugin *name* and format *fmt*."""
    want = _norm(name)
    for dev in root.iter():
        if dev.tag in ("PluginDevice", "AuPluginDevice"):
            if _device_format(dev) == fmt and _device_name(dev) and _norm(_device_name(dev)) == want:
                return dev
    return None


def synthesize_au_device(donor: ET.Element, *, name: str, manufacturer: str,
                         comp_type: int, comp_subtype: int, comp_manufacturer: int,
                         preset_plist: dict) -> ET.Element:
    """Build an AU device node for a plugin that was never used as an AU in any
    saved project (so no real template exists to harvest).

    *donor* is a real ``AuPluginDevice`` node from the same vendor/framework —
    its wrapper structure is kept, but the component identity, stored preset
    and parameter list are replaced. *preset_plist* should be the plugin's
    default ``kAudioUnitProperty_ClassInfo`` dict (dump it with
    ``tools/audump.c``), or, if the AU can't be instantiated headless, a
    minimal dict following the vendor's known key layout — the state key's
    value is overwritten during porting anyway.

    The donor's parameter list is blanked (its names/ids belong to a different
    plugin); Ableton re-queries parameters when it loads the AU.
    """
    import binascii as _ba
    import plistlib as _pl

    dev = copy.deepcopy(donor)
    info = dev.find(".//AuPluginInfo")

    def setv(parent, tag, val):
        e = parent.find(tag)
        if e is not None:
            e.set("Value", str(val))

    setv(info, "ComponentType", comp_type)
    setv(info, "ComponentSubType", comp_subtype)
    setv(info, "ComponentManufacturer", comp_manufacturer)
    setv(info, "Name", name)
    setv(info, "Manufacturer", manufacturer)

    pre = info.find(".//AuPreset")
    setv(pre, "Name", preset_plist.get("name", "Default"))
    setv(pre, "Manufacturer", comp_manufacturer)
    setv(pre, "SubType", comp_subtype)
    setv(pre, "Type", comp_type)
    buf = pre.find("Buffer")
    buf.text = _ba.hexlify(_pl.dumps(preset_plist, fmt=_pl.FMT_XML)).decode().upper()
    # drop the donor's preset-file pointer — it names the wrong plugin's cache
    pr = pre.find("PresetRef")
    if pr is not None:
        for c in list(pr):
            pr.remove(c)

    for p in dev.findall(".//ParameterList/"):
        setv(p, "ParameterName", "")
        setv(p, "ParameterId", -1)
        setv(p, "VisualIndex", 1073741823)
    return dev


def harvest_templates(wanted: set, als_paths, cache_dir: Optional[Path] = None,
                      log=print) -> dict:
    """Find a clean device template for each (name, fmt) in *wanted*.

    Scans *als_paths* (Path iterable) until every wanted template is found.
    Returns {(name, fmt): ET.Element}. Missing ones are simply absent — the
    caller should then ask the user to instantiate them once.

    A *cache_dir* (optional) stores harvested nodes as ``<name>__<fmt>.xml`` so
    repeat runs skip the library scan.
    """
    out = {}
    remaining = set(wanted)

    if cache_dir:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        for (name, fmt) in list(remaining):
            f = cache_dir / f"{_norm(name)}__{fmt}.xml"
            if f.exists():
                out[(name, fmt)] = ET.fromstring(f.read_text())
                remaining.discard((name, fmt))
                log(f"  template (cache): {name} [{fmt}]")

    fmt_marker = {"VST3": b"Vst3PluginInfo", "AU": b"AuPluginInfo", "VST2": b"VstPluginInfo"}
    for path in als_paths:
        if not remaining:
            break
        try:
            data = gzip.open(path, "rb").read()
        except Exception:
            continue
        # cheap prefilter: only ET-parse files that could contain a wanted
        # device. Match on word fragments, not the exact name — the same
        # plugin's display name can differ per format ("LittleAlterBoy" VST2
        # vs "Little AlterBoy" AU).
        def _maybe_has(name, fmt):
            frags = re.findall(r"[A-Za-z0-9]{3,}", name) or [name]
            return (fmt_marker[fmt] in data
                    and all(f.encode() in data for f in frags))
        if not any(_maybe_has(name, fmt) for (name, fmt) in remaining):
            continue
        try:
            root = ET.fromstring(data)
        except Exception:
            continue
        for (name, fmt) in list(remaining):
            node = find_device_node(root, name, fmt)
            if node is not None:
                tpl = copy.deepcopy(node)
                out[(name, fmt)] = tpl
                remaining.discard((name, fmt))
                log(f"  template: {name} [{fmt}]  <- {Path(path).name}")
                if cache_dir:
                    (cache_dir / f"{_norm(name)}__{fmt}.xml").write_text(
                        ET.tostring(tpl, encoding="unicode"))
    return out
