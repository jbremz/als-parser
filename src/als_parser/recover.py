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
    port_state: bool = True               # False = deliberate different-plugin swap:
                                          # insert the template with its own state and
                                          # report the old settings for manual recall

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


def chunk_report(chunk: bytes) -> str:
    """Human-readable forensics for an unportable chunk: file references,
    ASCII and UTF-16 strings (snapshot/preset names, tags, authors). Written
    as a sidecar next to each exported .vst2chunk."""
    import re
    lines = [f"chunk size: {len(chunk)} bytes",
             f"container head: {chunk[:16]!r}", ""]
    asc = [s.decode("latin-1") for s in re.findall(rb"[ -~]{5,}", chunk)]
    paths = [s for s in asc if "file:" in s or "/" in s and "." in s]
    if paths:
        lines.append("file references:")
        lines += [f"  {p}" for p in dict.fromkeys(paths)][:40]
    u16 = [s.decode("utf-16-le")
           for s in re.findall(rb"(?:[\x20-\x7e]\x00){4,}", chunk)]
    names = [s for s in dict.fromkeys(u16)
             if not s.startswith("\\@") and len(s) < 60]
    if names:
        lines.append("")
        lines.append("embedded names/tags (snapshot & preset names often here):")
        lines += [f"  {s}" for s in names[:40]]
    other = [s for s in dict.fromkeys(asc)
             if s not in paths and not re.fullmatch(r"[A-Za-z0-9+/=]{8,}", s)
             and len(s) < 60][:25]
    if other:
        lines.append("")
        lines.append("other readable strings:")
        lines += [f"  {s}" for s in other]
    return "\n".join(lines) + "\n"


def _vst3_align(src: bytes, template_state_hex: str) -> Optional[int]:
    """Decide whether a VST2 chunk can be ported into a VST3 ProcessorState, and
    at what byte offset. Returns the number of prefix bytes to strip from the
    VST2 chunk, or None if the formats differ.

    Vendor-verified rules (against reference states from real projects):
      - JUCE (`VC2!` magic both sides): state is wrapper-independent -> 0.
        (soothe2, and any plugin using copyXmlToBinary)
      - Identical first-6-bytes (magic + format version) -> 0. Passes NI
        Transient Master; correctly rejects Reaktor (CSAR v5 vs v6 differ at
        the version byte inside the first 6).
      - Size-prefixed both sides (bytes 0-4 are a content-dependent size,
        structure matches at 4..16) -> 0. Waves ('TAPS' chunks).
      - VST2 = 4/8-byte prefix + the exact VST3 stream -> strip the prefix.
        iZotope DDLY (VST2[4:12] == VST3[0:8]).
    """
    if not template_state_hex:
        return None
    try:
        tpl = binascii.unhexlify("".join(template_state_hex.split()))
    except binascii.Error:
        return None
    if tpl[:4] == b"VstW":
        # Steinberg's VST2-compat preset wrapper: the VST3 accepts its old VST2
        # chunk wrapped as VstW + CcnK(FBCh/FPCh). Rebuildable around src.
        return "vstw" if _wrap_vstw(tpl, src) is not None else None
    if src[:4] == b"VC2!" and tpl[:4] == b"VC2!":
        return 0
    if src[:6] == tpl[:6]:
        return 0
    if src[4:16] == tpl[4:16] and src[4:16] != b"\x00" * 12:
        return 0
    for off in (4, 8):
        if len(src) > off + 8 and src[off:off + 8] == tpl[:8]:
            return off
    if _izotope_rewrap(src, tpl) is not None:
        return "izotope"
    return None


def _izotope_rewrap(src: bytes, tpl: bytes) -> Optional[bytes]:
    """iZotope (Ozone-era) state: a zlib blob of a JSON 'Context State' dict.
    VST2 wraps it in an old 20-byte header; AU/VST3 use
    ``<u32 magic><u32 version><u32 zlib_len+4><u32 raw_len>`` + zlib (verified
    empirically: both sides decompress to the identical JSON). Rebuild the
    modern wrapper (magic/version copied from the template) around the VST2
    chunk's own zlib payload. Returns None if either side isn't this shape."""
    import struct as _st
    import zlib as _zl
    if len(tpl) < 18 or tpl[16:18] != b"\x78\x9c":
        return None
    off = src.find(b"\x78\x9c", 0, 64)
    if off < 0:
        return None
    try:
        raw_len = len(_zl.decompress(src[off:]))
    except _zl.error:
        return None
    payload = src[off:]
    return tpl[:8] + _st.pack("<II", len(payload) + 4, raw_len) + payload


def _wrap_vstw(tpl_state: bytes, src_chunk: bytes) -> Optional[bytes]:
    """Rebuild a VstW-wrapped VST2 bank/preset container around *src_chunk*,
    copying the template's headers. Layout: VstW(16B) + CcnK + 'FBCh' (bank,
    v2: 160B header, chunkSize at [172:176]) or 'FPCh' (preset: 60B header,
    chunkSize at [72:76]) + chunk. Returns None if the template isn't this
    shape or the inner chunk class doesn't match the source's."""
    import struct as _st
    if tpl_state[:4] != b"VstW" or tpl_state[16:20] != b"CcnK":
        return None
    kind = tpl_state[24:28]
    hdr_end = {b"FBCh": 176, b"FPCh": 76}.get(kind)
    if hdr_end is None or len(tpl_state) < hdr_end:
        return None
    tpl_inner = tpl_state[hdr_end:]
    if (src_chunk[:4] == b"VC2!") != (tpl_inner[:4] == b"VC2!"):
        return None
    head = bytearray(tpl_state[:hdr_end])
    _st.pack_into(">I", head, hdr_end - 4, len(src_chunk))          # chunkSize
    _st.pack_into(">I", head, 20, hdr_end - 24 + len(src_chunk))    # CcnK byteSize
    return bytes(head) + src_chunk


def _replace_child(parent: ET.Element, old: ET.Element, new: ET.Element) -> None:
    """Swap *old* for *new* at the same position, preserving indentation."""
    idx = list(parent).index(old)
    new.tail = old.tail
    parent[idx] = new


def _unique_sibling_id(parent: ET.Element, elem: ET.Element) -> None:
    """Give *elem* an Id unique among ALL of its siblings.

    Ableton list ids are scoped per parent element and tag-agnostic — two
    children of the same parent may never share an Id, whatever their tags
    (loading fails with 'Non-unique list ids'). A spliced-in device node
    carries its donor's Id, so it must be re-assigned.
    """
    used = set()
    for sib in parent:
        if sib is elem:
            continue
        v = sib.get("Id")
        if v is not None and v.lstrip("-").isdigit():
            used.add(int(v))
    elem.set("Id", str(max(used, default=-1) + 1))


def find_sibling_id_collisions(root: ET.Element) -> list:
    """All (parent_tag, id, [child_tags]) where siblings share an Id."""
    from collections import defaultdict
    out = []
    for parent in root.iter():
        ids = defaultdict(list)
        for c in parent:
            v = c.get("Id")
            if v is not None:
                ids[v].append(c.tag)
        out.extend((parent.tag, v, tags) for v, tags in ids.items() if len(tags) > 1)
    return out


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
    off = _vst3_align(src_chunk, tpl_ps.text or "")
    if off is None:
        return False, "VST2/VST3 chunk formats differ (needs manual recall)"

    tpl_bytes = binascii.unhexlify("".join((tpl_ps.text or "").split()))
    if off == "vstw":
        new_state = _wrap_vstw(tpl_bytes, src_chunk)
        note = " (VstW-wrapped)"
    elif off == "izotope":
        new_state = _izotope_rewrap(src_chunk, tpl_bytes)
        note = " (iZotope zlib rewrap)"
    else:
        new_state = src_chunk[off:]
        note = f" (stripped {off}B prefix)" if off else ""
    new_pd = copy.deepcopy(tpl.find("PluginDesc"))
    T.remap_pointee_ids(root, new_pd)
    _strip_preset_refs(new_pd)
    new_pd.find(".//Vst3Preset/ProcessorState").text = _hex(new_state)
    _replace_child(dev, dev.find("PluginDesc"), new_pd)

    old_sc, tpl_sc = dev.find("SourceContext"), tpl.find("SourceContext")
    if old_sc is not None and tpl_sc is not None:
        new_sc = copy.deepcopy(tpl_sc)
        _strip_preset_refs(new_sc)
        _replace_child(dev, old_sc, new_sc)

    mpe = dev.find("MpePitchBendUsesTuning")
    if mpe is not None:
        mpe.set("Value", "true")
    _carry_on_state(dev, dev)   # grafted Vst3Preset IsOn follows the wrapper
    return True, (f"{len(new_state)}B -> ProcessorState{note} "
                  f"(wrapper kept, automation intact)")


def _rebuild_fpch(template_fxp: bytes, chunk: bytes) -> bytes:
    """Rebuild a CcnK/FPCh (opaque-chunk preset) container around *chunk*,
    keeping the template's identity header. Layout: CcnK, byteSize BE,
    'FPCh', version, fxID, fxVersion, numPrograms, prgName[28],
    chunkSize BE, chunk."""
    import struct as _st
    head = bytearray(template_fxp[:60])
    _st.pack_into(">I", head, 56, len(chunk))
    body = bytes(head[8:]) + chunk
    return b"CcnK" + _st.pack(">I", len(body)) + body


def _port_au(new_dev: ET.Element, src_chunk: bytes, src_params: dict,
             param_map: Optional[dict], program: Optional[int] = None) -> tuple:
    buf, plist = au_plist(new_dev)
    if "vstdata" in plist:
        vd = plist["vstdata"]
        if vd[:4] != b"CcnK":
            # raw VST2 chunk stored directly (NI Reaktor: '\x01'+'4RIN'+ver).
            # Same-family check on the magic only — the plugin upgrades older
            # chunk versions itself (verified for Reaktor via audump --set).
            if vd[:5] != src_chunk[:5]:
                return False, "vstdata raw-chunk magic differs from VST2 chunk"
            plist["vstdata"] = src_chunk
            set_au_plist(buf, plist)
            return True, f"vstdata raw chunk {len(src_chunk)}B"
        if vd[8:12] == b"FPCh":                    # opaque-chunk preset (bank)
            plist["vstdata"] = _rebuild_fpch(vd, src_chunk)
            set_au_plist(buf, plist)
            return True, f"vstdata FPCh chunk {len(src_chunk)}B"
        fxp = FXP(vd)                              # soundhack-style FxCk params
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
    if "mCompleteData" in plist:                   # Rob Papen bank blob
        tpl = plist["mCompleteData"] or b""
        if tpl and src_chunk[:4].isascii() != tpl[:4].isascii():
            return False, "source/template mCompleteData shapes differ"
        plist["mCompleteData"] = src_chunk
        if program is not None and "mCurPreset" in plist:
            plist["mCurPreset"] = program
        set_au_plist(buf, plist)
        return True, f"mCompleteData {len(src_chunk)}B, preset #{program}"
    if "AM_STATE" in plist:                        # u-he text patch
        m = src_chunk.find(b"#AM=")
        if m < 0:
            return False, "source patch has no #AM= marker"
        plist["AM_STATE"] = src_chunk[m:]
        set_au_plist(buf, plist)
        return True, f"AM_STATE {len(plist['AM_STATE'])}B"
    if "soundtoys-data" in plist:                  # Soundtoys WIDGET text
        if not src_chunk.startswith(b"WIDGET"):
            return False, "source chunk is not Soundtoys WIDGET text"
        text = (src_chunk.decode("latin-1").rstrip("\x00")
                .replace("\r\n", "\n").replace("\r", "\n"))
        plist["soundtoys-data"] = text
        set_au_plist(buf, plist)
        return True, f"soundtoys-data {len(text)}B"
    if "jucePluginState" in plist:
        # JUCE getStateInformation output. Usually a VC2!-wrapped XML blob
        # (copyXmlToBinary), but some plugins return raw bytes (Klevgrand
        # Gaffel: a bare float dump). Either way the same blob is stored on
        # both the VST2 and AU sides — port it if src and template agree on
        # which class they are.
        tpl_state = plist["jucePluginState"] or b""
        if (src_chunk[:4] == b"VC2!") != (tpl_state[:4] == b"VC2!"):
            return False, "source/template jucePluginState formats differ"
        plist["jucePluginState"] = src_chunk
        set_au_plist(buf, plist)
        return True, f"jucePluginState {len(src_chunk)}B"
    state_keys = set(plist) - {"manufacturer", "subtype", "type", "version",
                               "name", "ProgramNumber", "element-name",
                               "mCurPreset", "data"}
    if "data" in plist and not state_keys:
        # iZotope-style AU: the whole state lives in the standard 'data' key
        # as the same stream the plugin's VST3 uses. Reuse the VST3 alignment
        # rules against the template's default data.
        tpl_data = plist["data"] or b""
        off = _vst3_align(src_chunk, binascii.hexlify(tpl_data).decode())
        if off is None:
            return False, "AU 'data' stream format differs from VST2 chunk"
        if off == "izotope":
            plist["data"] = _izotope_rewrap(src_chunk, tpl_data)
            note = "iZotope zlib rewrap"
        elif off == "vstw":
            return False, "unexpected VstW template in AU data"
        else:
            plist["data"] = src_chunk[off:]
            note = f"stripped {off}B prefix" if off else "verbatim"
        set_au_plist(buf, plist)
        return True, f"AU data {len(plist['data'])}B ({note})"
    return False, ("unknown AU state layout (no vstdata/AM_STATE/"
                   "soundtoys-data/jucePluginState/mCompleteData/data)")


def _strip_preset_refs(node: ET.Element) -> None:
    """Clear preset-file pointers in a grafted template node.

    Templates harvested from pre-Live-11 projects carry old-style FileRefs
    (``RelativePath`` containing ``RelativePathElement`` children, no Value
    attribute) inside LastPresetRef / PresetRef. Live 12 refuses to load them
    ("Required attribute 'Value' missing"). These are cosmetic browser
    pointers, not plugin state — reduce them to the modern empty forms:
    ``<LastPresetRef><Value /></LastPresetRef>`` and ``<PresetRef />``.
    """
    for lpr in node.iter("LastPresetRef"):
        for c in list(lpr):
            lpr.remove(c)
        ET.SubElement(lpr, "Value")
    for pr in node.iter("PresetRef"):
        for c in list(pr):
            pr.remove(c)


def _automation_target_map(old_dev: ET.Element, new_dev: ET.Element,
                           param_map: Optional[dict]) -> dict:
    """Map old-device AutomationTarget ids -> new-device ids so envelopes can be
    relinked when a device node is replaced. Matches the On switch directly and
    parameters by name — exact, via param_map, or by prefix (VST2 truncates
    parameter names to 15 chars; the AU exposes the full name)."""
    m = {}
    o_on = old_dev.find("./On/AutomationTarget")
    n_on = new_dev.find("./On/AutomationTarget")
    if o_on is not None and n_on is not None:
        m[o_on.get("Id")] = n_on.get("Id")

    def targets(d):
        out = {}
        for p in d.findall(".//ParameterList/"):
            pn, at = p.find("ParameterName"), p.find(".//AutomationTarget")
            if pn is not None and at is not None and pn.get("Value"):
                out[pn.get("Value")] = at.get("Id")
        return out

    old_t, new_t = targets(old_dev), targets(new_dev)
    inv = {v: k for k, v in (param_map or {}).items()}   # src name -> dest name
    for oname, oid in old_t.items():
        cands = [inv.get(oname), oname if oname in new_t else None]
        if ": " in oname:
            # u-he style: VST2 prefixes names with the product ("Tyrell: Tune2")
            # while the AU exposes the bare name ("Tune2")
            cands.append(oname.split(": ", 1)[1])
        if len(oname) >= 8:   # VST2 15-char truncation
            cands += [n for n in new_t if n.startswith(oname)]
        for c in cands:
            if c and c in new_t:
                m[oid] = new_t[c]
                break
    return m


def _carry_on_state(old_dev: ET.Element, new_dev: ET.Element) -> None:
    """Copy the device activator (On switch) from the old device to its
    replacement. Replacement nodes are cloned from templates whose donor may
    have been switched off in its source project — without this, ported
    devices arrive deactivated."""
    src = old_dev.find("./On/Manual")
    val = src.get("Value") if src is not None else "true"
    dst = new_dev.find("./On/Manual")
    if dst is not None:
        dst.set("Value", val)
    for tag in ("AuPreset", "Vst3Preset"):
        ison = new_dev.find(f".//{tag}/IsOn")
        if ison is not None:
            ison.set("Value", val)


def _process_device(root: ET.Element, pmap: dict, dev: ET.Element,
                    spec: "PluginSpec", tpl: ET.Element, tname: str,
                    exports: Optional[list] = None) -> Action:
    """Convert one dead VST2 device according to *spec*. Mutates the tree.

    On failure the device is left untouched (dead but data-intact), the action
    carries a manual-recall hint extracted from the chunk, and the chunk is
    added to *exports* so it can be written out as a recovery artifact.
    """
    src_chunk = vst2_chunk(dev)
    target = f"{spec.target_name} [{spec.target_fmt}]"

    def skipped(detail):
        refs = _extract_refs(src_chunk)
        if exports is not None:
            exports.append((tname, spec.vst2_name, src_chunk))
        return Action(tname, spec.vst2_name, target, "skipped",
                      detail + (f"; recall: {refs}" if refs else ""))

    if not spec.port_state:
        # deliberate different-plugin swap: insert the template with its own
        # state; export the old chunk so its settings can be matched by hand
        new_dev = copy.deepcopy(tpl)
        T.remap_pointee_ids(root, new_dev)
        _strip_preset_refs(new_dev)
        if new_dev.tag == "PluginDevice" and dev.tag == "PluginDevice":
            # keep the old wrapper (automation ids etc.), graft the plugin
            new_pd = new_dev.find("PluginDesc")
            _replace_child(dev, dev.find("PluginDesc"), copy.deepcopy(new_pd))
            sc = new_dev.find("SourceContext")
            if sc is not None and dev.find("SourceContext") is not None:
                _replace_child(dev, dev.find("SourceContext"), copy.deepcopy(sc))
            mpe = dev.find("MpePitchBendUsesTuning")
            if mpe is not None:
                mpe.set("Value", "true")
            _carry_on_state(dev, dev)
        else:
            _carry_on_state(dev, new_dev)
            _replace_child(pmap[dev], dev, new_dev)
            pmap[new_dev] = pmap[dev]
            _unique_sibling_id(pmap[new_dev], new_dev)
        if exports is not None:
            exports.append((tname, spec.vst2_name, src_chunk))
        return Action(tname, spec.vst2_name, target, "replaced",
                      "different plugin, state NOT ported — match by hand "
                      "(old settings exported)")

    if spec.target_fmt == "VST3":
        ok, detail = _convert_vst3_inplace(root, dev, tpl, src_chunk)
        if not ok:
            return skipped(detail)
        return Action(tname, spec.vst2_name, target, "ported", detail)

    # AU: tag + parameter space differ, so the whole node is replaced
    new_dev = copy.deepcopy(tpl)
    T.remap_pointee_ids(root, new_dev)
    _strip_preset_refs(new_dev)
    prog_e = dev.find(".//VstPreset/ProgramNumber")
    program = int(prog_e.get("Value")) if prog_e is not None and prog_e.get("Value") else None
    ok, detail = _port_au(new_dev, src_chunk, param_values(dev), spec.param_map,
                          program=program)
    if not ok:
        return skipped(detail)

    old_defs = {x.get("Id") for x in dev.iter()
                if "Id" in x.attrib and T.is_pointee_def(x.tag)}
    tmap = _automation_target_map(dev, new_dev, spec.param_map)
    _carry_on_state(dev, new_dev)
    _replace_child(pmap[dev], dev, new_dev)
    pmap[new_dev] = pmap[dev]
    _unique_sibling_id(pmap[new_dev], new_dev)

    # relink automation envelopes that pointed at the old device
    relinked = orphans = 0
    for x in root.iter():
        if x.tag == "PointeeId" and x.get("Value") in old_defs:
            if x.get("Value") in tmap:
                x.set("Value", tmap[x.get("Value")])
                relinked += 1
            else:
                orphans += 1
    if relinked:
        detail += f"; relinked {relinked} automation env(s)"
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
    baseline_legacy = sum(1 for _ in root.iter("RelativePathElement"))

    # tracks (incl. their nesting) that actually contain an affected device.
    # Group/Return/Main tracks host devices too (sends and master chains).
    # MasterTrack is the pre-Live-12 name for MainTrack.
    SCAN_TAGS = ("AudioTrack", "MidiTrack", "GroupTrack", "ReturnTrack",
                 "MainTrack", "MasterTrack")
    tracks = [t for t in root.iter() if t.tag in SCAN_TAGS
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
    exports = []
    for track in tracks:
        ne = track.find(".//Name/EffectiveName")
        tname = (ne.get("Value") if ne is not None and ne.get("Value")
                 else track.tag.replace("Track", ""))
        if mode == "duplicate" and track.tag in ("AudioTrack", "MidiTrack"):
            work = T.duplicate_track(root, track, new_name=f"{tname} - COMPAT",
                                     mute=True)
        else:
            # Group/Return/Main tracks can't be meaningfully duplicated —
            # convert their devices in place even in duplicate mode.
            work = track

        pmap = _parent_map(work)
        for dev in _affected_vst2_devices(work, names):
            spec = specs_by_name[_device_name(dev)]
            tpl = templates.get((spec.target_name, spec.target_fmt))
            if tpl is None:
                exports.append((tname, spec.vst2_name, vst2_chunk(dev)))
                actions.append(Action(
                    tname, spec.vst2_name, f"{spec.target_name} [{spec.target_fmt}]",
                    "skipped", "no device template"))
                continue
            actions.append(_process_device(root, pmap, dev, spec, tpl, tname, exports))

    # report
    log("\nActions:")
    for a in actions:
        flag = {"ported": "OK ", "replaced": "SWAP", "skipped": "SKIP"}.get(a.status, "?")
        log(f"  [{flag}] {a.track[:24]:24} {a.plugin:18} -> {a.target:24} {a.detail}")

    # validate + write: must serialise, re-parse, and have no sibling-id
    # collisions (Ableton refuses to load 'Non-unique list ids')
    import io
    buf = io.BytesIO()
    tree.write(buf, encoding="utf-8")
    ET.fromstring(buf.getvalue())
    collisions = find_sibling_id_collisions(root)
    if collisions:
        for ptag, v, tags in collisions[:10]:
            log(f"  ID COLLISION in <{ptag}>: Id={v} shared by {tags}")
        raise RuntimeError(
            f"{len(collisions)} sibling-id collision(s) — refusing to write")
    # era gate: grafted templates from pre-Live-11 projects carry old-style
    # path serialisation (RelativePathElement) that Live 12 refuses to load.
    # We never add any — if the count grew, a graft wasn't sanitised.
    n_legacy = sum(1 for _ in root.iter("RelativePathElement"))
    if n_legacy > baseline_legacy:
        raise RuntimeError(
            f"legacy RelativePathElement count grew {baseline_legacy} -> "
            f"{n_legacy} — unsanitised old-era template; refusing to write")
    log("\nXML re-parse: OK; sibling ids unique; no legacy path elements added")

    if apply:
        if exports:
            # skipped devices keep their dead VST2 in the file, but also export
            # the raw chunks as artifacts for out-of-band recovery
            rec_dir = (out_path or als_path).parent / "_RECOVERY"
            rec_dir.mkdir(exist_ok=True)
            import re as _re
            for tname, plugin, chunk in exports:
                safe = _re.sub(r"[^\w\- ]", "_", f"{plugin} - {tname}")[:80]
                (rec_dir / f"{safe}.vst2chunk").write_bytes(chunk)
                (rec_dir / f"{safe}.txt").write_text(chunk_report(chunk))
            log(f"Exported {len(exports)} skipped chunk(s) to {rec_dir}")
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
