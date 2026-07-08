"""End-to-end VST2 recovery for Ableton projects.

Given a project .als and a list of affected (dead) VST2 plugins plus the format
to move each to, this:

  1. finds every track containing an affected VST2 device,
  2. depending on *mode*:
       - "inplace" (default): converts the dead devices directly on their own
         tracks and writes the result to a NEW .als next to the original
         ("<name> [recovered].als") — the original file is never touched;
       - "duplicate": duplicates each affected track (muted, "- COMPAT"),
         exactly like Ableton's Cmd-D, and converts the copies, writing back
         to the same file (with a safety backup),
  3. swaps each dead VST2 device for the chosen VST3/AU replacement
     (identity taken from a harvested device template), and
  4. ports the old preset state across with the method that fits the target:
       - VST3: graft the template's PluginDesc onto the existing device wrapper
         and copy the VST2 chunk into ProcessorState *iff* the formats match.
         Keeping the wrapper preserves the ParameterList and its
         AutomationTargets, so parameter automation survives the swap.
       - AU: replace the whole device node (the tag and parameter space differ);
         state goes into the .aupreset plist — soundhack's `vstdata` FXP floats
         are rewritten from the VST2 params, u-he's `AM_STATE` gets the text
         patch. Automation that pointed at the old device is reported for
         manual relinking.

Anything it can't do confidently (incompatible chunk formats, missing template,
unknown AU state layout) is reported — and the dead VST2 left in place with a
recall hint — never silently botched.
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


def _replace_child(parent: ET.Element, old: ET.Element, new: ET.Element) -> None:
    """Swap *old* for *new* at the same position, preserving indentation."""
    idx = list(parent).index(old)
    new.tail = old.tail
    parent[idx] = new


def _convert_vst3_inplace(root: ET.Element, dev: ET.Element, tpl: ET.Element,
                          src_chunk: bytes) -> tuple:
    """Convert a VST2 PluginDevice to VST3 by grafting the template's PluginDesc
    (and SourceContext) onto the existing wrapper, then writing the chunk into
    ProcessorState. The wrapper — ParameterList, AutomationTargets, On switch —
    is kept, so parameter automation keeps working.

    The grafted PluginDesc carries pointee defs (ControllerTargets.N inside
    Vst3Preset), so it gets fresh ids like any spliced subtree.
    """
    tpl_ps = tpl.find(".//Vst3Preset/ProcessorState")
    if tpl_ps is None:
        return False, "no ProcessorState in template"
    if not _vst3_format_compatible(src_chunk, tpl_ps.text or ""):
        return False, "VST2/VST3 chunk formats differ (needs manual recall)"

    new_pd = copy.deepcopy(tpl.find("PluginDesc"))
    T.remap_pointee_ids(root, new_pd)
    new_pd.find(".//Vst3Preset/ProcessorState").text = _hex(src_chunk)
    _replace_child(dev, dev.find("PluginDesc"), new_pd)

    old_sc, tpl_sc = dev.find("SourceContext"), tpl.find("SourceContext")
    if old_sc is not None and tpl_sc is not None:
        _replace_child(dev, old_sc, copy.deepcopy(tpl_sc))

    mpe = dev.find("MpePitchBendUsesTuning")
    if mpe is not None:
        mpe.set("Value", "true")
    return True, f"{len(src_chunk)}B -> ProcessorState (wrapper kept, automation intact)"


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


def _process_device(root: ET.Element, pmap: dict, dev: ET.Element,
                    spec: "PluginSpec", tpl: ET.Element, tname: str) -> Action:
    """Convert one dead VST2 device according to *spec*. Mutates the tree.

    On failure the device is left untouched (dead but data-intact) and the
    action carries a manual-recall hint extracted from the chunk.
    """
    src_chunk = vst2_chunk(dev)
    target = f"{spec.target_name} [{spec.target_fmt}]"

    if spec.target_fmt == "VST3":
        ok, detail = _convert_vst3_inplace(root, dev, tpl, src_chunk)
        if not ok:
            refs = _extract_refs(src_chunk)
            return Action(tname, spec.vst2_name, target, "skipped",
                          detail + (f"; recall: {refs}" if refs else ""))
        return Action(tname, spec.vst2_name, target, "ported", detail)

    # AU: tag + parameter space differ, so the whole node is replaced
    new_dev = copy.deepcopy(tpl)
    T.remap_pointee_ids(root, new_dev)
    ok, detail = _port_au(new_dev, src_chunk, param_values(dev), spec.param_map)
    if not ok:
        refs = _extract_refs(src_chunk)
        return Action(tname, spec.vst2_name, target, "skipped",
                      detail + (f"; recall: {refs}" if refs else ""))

    old_defs = {x.get("Id") for x in dev.iter()
                if "Id" in x.attrib and T.is_pointee_def(x.tag)}
    _replace_child(pmap[dev], dev, new_dev)
    pmap[new_dev] = pmap[dev]

    orphans = sum(1 for x in root.iter()
                  if x.tag == "PointeeId" and x.get("Value") in old_defs)
    if orphans:
        detail += f"; WARNING: {orphans} automation ref(s) need manual relink"
    return Action(tname, spec.vst2_name, target, "ported", detail)


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
                    cache_dir=None, apply=False, log=print,
                    mode: str = "inplace", output=None) -> list:
    """Run recovery. Returns a list[Action]. Writes only if apply=True.

    mode="inplace" (default): convert devices on their own tracks, write to a
    NEW file (*output*, default "<name> [recovered].als") — original untouched.
    mode="duplicate": convert on muted "- COMPAT" track copies, write back to
    the same file with a .pre-recover-bak safety copy.
    """
    if mode not in ("inplace", "duplicate"):
        raise ValueError(f"unknown mode {mode!r}")
    als_path = Path(als_path)
    out_path = None
    if mode == "inplace":
        out_path = Path(output) if output else als_path.with_name(
            f"{als_path.stem} [recovered].als")
        if out_path.resolve() == als_path.resolve():
            raise ValueError("inplace mode must write to a new file, not the original")
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
        if mode == "duplicate":
            work = T.duplicate_track(root, track, new_name=f"{tname} - COMPAT",
                                     mute=True)
        else:
            work = track

        pmap = _parent_map(work)
        for dev in _affected_vst2_devices(work, names):
            spec = specs_by_name[_device_name(dev)]
            tpl = templates.get((spec.target_name, spec.target_fmt))
            if tpl is None:
                actions.append(Action(
                    tname, spec.vst2_name, f"{spec.target_name} [{spec.target_fmt}]",
                    "skipped", "no device template"))
                continue
            actions.append(_process_device(root, pmap, dev, spec, tpl, tname))

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
        if mode == "inplace":
            save_als(tree, out_path)
            log(f"Wrote {out_path}  (original untouched)")
        else:
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
