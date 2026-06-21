"""End-to-end VST2 recovery for Ableton projects.

Given a project .als and a list of affected (dead) VST2 plugins plus the format
to move each to, this:

  1. finds every track containing an affected VST2 device,
  2. duplicates that track (muted, "- COMPAT"), exactly like Ableton's Cmd-D,
  3. swaps each dead VST2 device in the copy for the chosen VST3/AU replacement
     (built from a harvested device template, ids remapped), and
  4. ports the old preset state across with the method that fits the target:
       - VST3: copy the VST2 chunk into ProcessorState *iff* the formats match,
       - AU soundhack: rewrite the embedded FXP floats from the VST2 params,
       - AU u-he: copy the text patch into AM_STATE.

Anything it can't do confidently (incompatible chunk formats, missing template,
unknown AU state layout) is reported, never silently botched.

The original tracks are left untouched; everything happens on the duplicates.
"""

from __future__ import annotations

import binascii
import copy
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import track_ops as T
from .device_templates import (
    installed_formats, harvest_templates, find_device_node, _device_name, _device_format,
)
from .preset_port import (
    load_als, save_als, vst2_chunk, param_values, param_order,
    au_plist, set_au_plist, FXP, _hex,
)


@dataclass
class PluginSpec:
    vst2_name: str
    target_fmt: str                       # "VST3" or "AU"
    target_name: Optional[str] = None     # replacement plugin name (default: vst2_name)
    param_map: Optional[dict] = None      # AU dest-param -> VST2 src-param (renamed plugins)

    def __post_init__(self):
        self.target_name = self.target_name or self.vst2_name


@dataclass
class Action:
    track: str
    plugin: str
    target: str
    status: str          # "ported" | "swapped-no-state" | "skipped"
    detail: str = ""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _parent_map(elem: ET.Element) -> dict:
    return {c: p for p in elem.iter() for c in p}


def _affected_vst2_devices(track: ET.Element, names: set) -> list:
    out = []
    for dev in track.iter():
        if dev.tag == "PluginDevice" and _device_format(dev) == "VST2":
            nm = _device_name(dev)
            if nm in names:
                out.append(dev)
    return out


def _extract_refs(chunk: bytes) -> str:
    """Readable ensemble/preset/file references inside a plugin chunk (for the
    manual-recall hint when we can't port automatically)."""
    import re
    hits = []
    for s in re.findall(rb"[ -~]{5,}", chunk):
        d = s.decode("latin-1")
        if any(k in d for k in (".ens", ".nmsv", ".fxp", ".aupreset", "file:")):
            d = d.split("/")[-1]
            if d and d not in hits:
                hits.append(d)
    return ", ".join(hits[:3])


def _vst3_format_compatible(vst2_chunk_bytes: bytes, template_state_hex: str) -> bool:
    """Heuristic: the VST2 chunk and the VST3 ProcessorState share a structural
    signature (first 6 bytes encode magic + format version for NI-style chunks).
    Catches Reaktor (CSAR v5 vs v6 -> version byte differs) while passing
    Transient Master (identical serialisation)."""
    if not template_state_hex:
        return False
    try:
        tpl = binascii.unhexlify("".join(template_state_hex.split()))
    except binascii.Error:
        return False
    return vst2_chunk_bytes[:6] == tpl[:6]


def _port_vst3(new_dev: ET.Element, src_chunk: bytes) -> tuple:
    ps = new_dev.find(".//Vst3Preset/ProcessorState")
    if ps is None:
        return False, "no ProcessorState in template"
    if not _vst3_format_compatible(src_chunk, ps.text or ""):
        return False, "VST2/VST3 chunk formats differ (needs manual recall)"
    ps.text = _hex(src_chunk)
    return True, f"{len(src_chunk)}B -> ProcessorState"


def _port_au(new_dev: ET.Element, src_chunk: bytes, src_params: dict,
             param_map: Optional[dict]) -> tuple:
    buf, plist = au_plist(new_dev)
    if "vstdata" in plist:                         # soundhack-style FXP
        fxp = FXP(plist["vstdata"])
        order = param_order(new_dev)
        floats = fxp.floats
        mapping = param_map or {n: n for n in order}
        applied = 0
        for i, pname in enumerate(order[: fxp.num_params]):
            src = mapping.get(pname)
            if src and src in src_params:
                floats[i] = src_params[src]
                applied += 1
        plist["vstdata"] = fxp.with_floats(floats).raw
        set_au_plist(buf, plist)
        return True, f"FXP: {applied} params"
    if "AM_STATE" in plist:                        # u-he text patch
        m = src_chunk.find(b"#AM=")
        if m < 0:
            return False, "source patch has no #AM= marker"
        plist["AM_STATE"] = src_chunk[m:]
        set_au_plist(buf, plist)
        return True, f"AM_STATE {len(plist['AM_STATE'])}B"
    return False, "unknown AU state layout (no vstdata/AM_STATE)"


# --------------------------------------------------------------------------- #
# analyze (semi-agentic: discover what's there, suggest a spec)
# --------------------------------------------------------------------------- #

def analyze_project(als_path: Path, log=print) -> list:
    """List every VST2 plugin in the project (all dead on a VST2-less Mac) with
    the track count and which replacement formats are installed. Returns a
    suggested spec list (default target: VST3 if installed, else AU)."""
    root = load_als(Path(als_path)).getroot()
    from collections import Counter
    counts = Counter()
    for dev in root.iter():
        if dev.tag == "PluginDevice" and _device_format(dev) == "VST2":
            counts[_device_name(dev)] += 1

    suggested = []
    log(f"VST2 plugins in {Path(als_path).name}:")
    for name, n in sorted(counts.items()):
        inst = installed_formats(name)
        fmt = "VST3" if "VST3" in inst else ("AU" if "AU" in inst else None)
        log(f"  {name:24} x{n:<3} installed: {', '.join(inst) or 'NONE'}"
            + (f"  -> suggest {fmt}" if fmt else "  -> no replacement found"))
        if fmt:
            suggested.append(PluginSpec(name, fmt))
    return suggested


# --------------------------------------------------------------------------- #
# main entry
# --------------------------------------------------------------------------- #

def recover_project(als_path: Path, specs, library_paths=None,
                    cache_dir=None, apply=False, log=print) -> list:
    """Run recovery. Returns a list[Action]. Writes the file only if apply=True."""
    als_path = Path(als_path)
    specs_by_name = {s.vst2_name: s for s in specs}
    names = set(specs_by_name)

    tree = load_als(als_path)
    root = tree.getroot()

    # tracks (incl. their nesting) that actually contain an affected device
    tracks = [t for t in root.iter() if t.tag in ("AudioTrack", "MidiTrack")
              if _affected_vst2_devices(t, names)]
    if not tracks:
        log("No affected VST2 devices found.")
        return []

    # report what we'll need, and check installs
    log(f"{len(tracks)} track(s) contain affected plugins: "
        + ", ".join(sorted(names)))
    for s in specs:
        inst = installed_formats(s.target_name)
        ok = s.target_fmt in inst
        log(f"  {s.vst2_name} -> {s.target_name} [{s.target_fmt}]  "
            f"installed: {'yes' if ok else 'NO — ' + (','.join(inst) or 'none')}")

    # harvest device templates (current project first, then library)
    wanted = {(s.target_name, s.target_fmt) for s in specs}
    scan = [als_path] + [Path(p) for p in (library_paths or [])]
    templates = harvest_templates(wanted, scan, cache_dir=cache_dir, log=log)
    for (name, fmt) in wanted - set(templates):
        log(f"  NO TEMPLATE for {name} [{fmt}] — instantiate it once in any "
            f"project (or pass its library path) and re-run.")

    actions = []
    for track in tracks:
        tname = track.find(".//Name/EffectiveName").get("Value")
        dup = T.duplicate_track(root, track, new_name=f"{tname} - COMPAT", mute=True)

        pmap = _parent_map(dup)
        for dev in _affected_vst2_devices(dup, names):
            spec = specs_by_name[_device_name(dev)]
            key = (spec.target_name, spec.target_fmt)
            tpl = templates.get(key)
            if tpl is None:
                actions.append(Action(tname, spec.vst2_name, f"{spec.target_name} [{spec.target_fmt}]",
                                      "skipped", "no device template"))
                continue

            src_chunk = vst2_chunk(dev)
            src_params = param_values(dev)
            new_dev = copy.deepcopy(tpl)
            T.remap_pointee_ids(root, new_dev)

            if spec.target_fmt == "VST3":
                ok, detail = _port_vst3(new_dev, src_chunk)
            else:
                ok, detail = _port_au(new_dev, src_chunk, src_params, spec.param_map)

            target = f"{spec.target_name} [{spec.target_fmt}]"
            if not ok:
                # can't port state confidently: leave the dead VST2 in place (no
                # data lost) and surface the recipe to recall it by hand.
                refs = _extract_refs(src_chunk)
                actions.append(Action(tname, spec.vst2_name, target, "skipped",
                                      detail + (f"; recall: {refs}" if refs else "")))
                continue

            # splice the new device in where the dead one was
            parent = pmap[dev]
            idx = list(parent).index(dev)
            new_dev.tail = dev.tail
            parent[idx] = new_dev
            pmap[new_dev] = parent
            actions.append(Action(tname, spec.vst2_name, target, "ported", detail))

    # report
    log("\nActions:")
    for a in actions:
        flag = {"ported": "OK ", "swapped-no-state": "WARN", "skipped": "SKIP"}.get(a.status, "?")
        log(f"  [{flag}] {a.track[:24]:24} {a.plugin:18} -> {a.target:24} {a.detail}")

    # validate + write
    import io
    buf = io.BytesIO()
    tree.write(buf, encoding="utf-8")
    ET.fromstring(buf.getvalue())
    log("\nXML re-parse: OK")

    if apply:
        bak = als_path.with_suffix(".als.pre-recover-bak")
        if not bak.exists():
            import shutil
            shutil.copy2(als_path, bak)
            log(f"Safety copy: {bak.name}")
        save_als(tree, als_path)
        log(f"Wrote {als_path}")
    else:
        log("\nDry run — re-run with apply=True to write.")
    return actions
