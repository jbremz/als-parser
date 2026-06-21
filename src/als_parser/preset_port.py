"""Port VST2 plugin preset state into VST3 / AudioUnit devices inside an .als file.

Background
----------
macOS dropped VST2 support, so VST2 plugins in old Ableton projects no longer
load. The preset/state information, however, is still stored verbatim inside the
``.als`` (a gzipped XML document). When you replace a dead VST2 device with the
*same plugin* in VST3 or AU form, Ableton inserts the new device with default
state. This module copies the old state across.

The authoritative state Ableton restores on load is the opaque plugin chunk, not
the mirrored ``ParameterList``. So porting == writing the correct chunk:

* VST2  -> VST3 : ``<VstPreset><Buffer>`` (raw plugin chunk, hex)
                  ->  ``<Vst3Preset><ProcessorState>`` (hex).
                  Only works when the plugin serialises VST2 and VST3 state in
                  the *same* binary format (e.g. NI Transient Master). Reaktor 6
                  does NOT (CSAR v5 vs v6) and cannot be ported this way.

* VST2  -> AU   : Ableton stores AU state as a ``.aupreset`` XML *plist* in
                  ``<AuPreset><Buffer>`` (hex). The plugin's real data lives in a
                  plist key:
                    - soundhack: ``vstdata`` = a VST2 FXP (``CcnK``/``FxCk``)
                      whose float array is the normalised parameter values.
                    - u-he:      ``AM_STATE`` = the text patch (identical across
                      all plugin formats).

Everything here is deliberately surgical: we locate the exact device by track +
plugin name + format, then rewrite a single element's text. ElementTree round
-trips this project losslessly (verified), so the rest of the file is untouched.
"""

from __future__ import annotations

import base64
import binascii
import copy
import gzip
import io
import plistlib
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# --------------------------------------------------------------------------- #
# .als (gzipped XML) I/O
# --------------------------------------------------------------------------- #

ABLETON_XML_DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'


def load_als(path: Path) -> ET.ElementTree:
    with gzip.open(path, "rb") as f:
        return ET.parse(f)


def save_als(tree: ET.ElementTree, path: Path) -> None:
    """Serialise the tree back to a gzipped .als, matching Ableton's wrapper."""
    buf = io.BytesIO()
    tree.write(buf, encoding="utf-8", xml_declaration=False)
    xml_bytes = ABLETON_XML_DECL.encode("utf-8") + buf.getvalue()
    if not xml_bytes.endswith(b"\n"):
        xml_bytes += b"\n"
    with gzip.open(path, "wb") as f:
        f.write(xml_bytes)


# --------------------------------------------------------------------------- #
# Device lookup
# --------------------------------------------------------------------------- #

def tracks_by_id(root: ET.Element) -> dict:
    return {
        t.get("Id"): t
        for t in root.iter()
        if t.tag in ("AudioTrack", "MidiTrack", "GroupTrack", "ReturnTrack")
    }


def device_format(dev: ET.Element) -> Optional[str]:
    if dev.find(".//VstPluginInfo") is not None:
        return "VST2"
    if dev.find(".//Vst3PluginInfo") is not None:
        return "VST3"
    if dev.find(".//AuPluginInfo") is not None:
        return "AU"
    return None


def device_name(dev: ET.Element) -> Optional[str]:
    for tag in ("VstPluginInfo/PlugName", "Vst3PluginInfo/Name", "AuPluginInfo/Name"):
        e = dev.find(".//" + tag)
        if e is not None and e.get("Value"):
            return e.get("Value")
    return None


def find_devices(track: ET.Element, fmt: str, name: str) -> list:
    """All plugin devices inside *track* (incl. nested in racks) of fmt+name."""
    out = []
    for dev in track.iter():
        if dev.tag in ("PluginDevice", "AuPluginDevice"):
            if device_format(dev) == fmt and device_name(dev) == name:
                out.append(dev)
    return out


def find_one(track: ET.Element, fmt: str, name: str, idx: int = 0) -> ET.Element:
    devs = find_devices(track, fmt, name)
    if not devs:
        raise LookupError(f"no {fmt} '{name}' device in track {track.get('Id')}")
    return devs[idx]


# --------------------------------------------------------------------------- #
# Raw chunk access
# --------------------------------------------------------------------------- #

def vst2_chunk(dev: ET.Element) -> bytes:
    buf = dev.find(".//VstPreset//Buffer")
    if buf is None or not buf.text:
        raise LookupError("VST2 device has no preset buffer")
    return binascii.unhexlify("".join(buf.text.split()))


def _hex(data: bytes) -> str:
    return binascii.hexlify(data).decode("ascii").upper()


def au_plist(dev: ET.Element) -> tuple:
    """Return (Buffer element, parsed plist dict) for an AU device."""
    buf = dev.find(".//AuPreset/Buffer")
    if buf is None or not buf.text:
        raise LookupError("AU device has no preset buffer")
    raw = binascii.unhexlify("".join(buf.text.split()))
    return buf, plistlib.loads(raw)


def set_au_plist(buf: ET.Element, plist: dict) -> None:
    raw = plistlib.dumps(plist, fmt=plistlib.FMT_XML)
    buf.text = _hex(raw)


# --------------------------------------------------------------------------- #
# Parameter values (from the plaintext ParameterList mirror)
# --------------------------------------------------------------------------- #

def param_values(dev: ET.Element) -> dict:
    """name -> float(Manual) for every PluginFloatParameter that has a value."""
    out = {}
    for p in dev.findall(".//ParameterList/PluginFloatParameter"):
        pn = p.find("ParameterName")
        mv = p.find(".//ParameterValue/Manual")
        if pn is not None and pn.get("Value") and mv is not None and mv.get("Value") is not None:
            try:
                out[pn.get("Value")] = float(mv.get("Value"))
            except ValueError:
                pass
    return out


def param_order(dev: ET.Element) -> list:
    """Ordered parameter names (FXP float order == ParameterList order)."""
    return [
        p.find("ParameterName").get("Value")
        for p in dev.findall(".//ParameterList/PluginFloatParameter")
        if p.find("ParameterName") is not None and p.find("ParameterName").get("Value")
    ]


# --------------------------------------------------------------------------- #
# FXP (VST2 preset) rewriting  -- used inside soundhack AU `vstdata`
# --------------------------------------------------------------------------- #

@dataclass
class FXP:
    raw: bytes

    @property
    def num_params(self) -> int:
        return struct.unpack(">I", self.raw[24:28])[0]

    @property
    def floats(self) -> list:
        n = self.num_params
        return list(struct.unpack(f">{n}f", self.raw[56:56 + 4 * n]))

    def with_floats(self, values: list) -> "FXP":
        n = self.num_params
        if len(values) != n:
            raise ValueError(f"expected {n} floats, got {len(values)}")
        new = bytearray(self.raw)
        new[56:56 + 4 * n] = struct.pack(f">{n}f", *values)
        return FXP(bytes(new))


# --------------------------------------------------------------------------- #
# High level ports
# --------------------------------------------------------------------------- #

@dataclass
class PortResult:
    label: str
    ok: bool
    detail: str = ""


def port_vst3_processorstate(src_dev: ET.Element, dst_dev: ET.Element, label: str) -> PortResult:
    """Copy a VST2 chunk verbatim into a VST3 device's ProcessorState.

    Only valid when both formats share the plugin's serialisation.
    """
    chunk = vst2_chunk(src_dev)
    ps = dst_dev.find(".//Vst3Preset/ProcessorState")
    if ps is None:
        return PortResult(label, False, "destination has no Vst3Preset/ProcessorState")
    ps.text = _hex(chunk)
    return PortResult(label, True, f"{len(chunk)} bytes -> ProcessorState")


def port_au_fxp_params(src_params: dict, dst_dev: ET.Element, mapping: dict, label: str) -> PortResult:
    """Rewrite a soundhack AU's `vstdata` FXP floats from source parameter values.

    *mapping* maps destination-AU param name -> source param name.
    """
    buf, plist = au_plist(dst_dev)
    if "vstdata" not in plist:
        return PortResult(label, False, "aupreset has no vstdata key")
    fxp = FXP(plist["vstdata"])
    order = param_order(dst_dev)
    floats = fxp.floats
    applied = []
    for i, pname in enumerate(order[: fxp.num_params]):
        if pname in mapping and mapping[pname] in src_params:
            floats[i] = src_params[mapping[pname]]
            applied.append(f"{pname}<-{mapping[pname]}={floats[i]:.4g}")
    plist["vstdata"] = fxp.with_floats(floats).raw
    set_au_plist(buf, plist)
    return PortResult(label, True, ", ".join(applied))


def _replace_child(parent: ET.Element, old: ET.Element, new: ET.Element) -> None:
    """Swap *old* for *new* at the same position, preserving indentation (tail)."""
    idx = list(parent).index(old)
    new.tail = old.tail
    parent[idx] = new


def convert_vst2_device_to_vst3(device: ET.Element, template_vst3: ET.Element,
                                chunk: bytes, label: str) -> PortResult:
    """Convert a VST2 ``<PluginDevice>`` to VST3 *in place*.

    Transplants ``<PluginDesc>`` and ``<SourceContext>`` from *template_vst3* (a
    known-good native VST3 device of the *same plugin*), then writes *chunk* into
    the new ProcessorState. The device wrapper — its globally-unique IDs and its
    ParameterList — is preserved, so this is only safe when those IDs are not
    referenced by automation elsewhere (check first), which means no remapping.
    """
    new_pd = copy.deepcopy(template_vst3.find("PluginDesc"))
    ps = new_pd.find(".//Vst3Preset/ProcessorState")
    if ps is None:
        return PortResult(label, False, "template has no Vst3Preset/ProcessorState")
    ps.text = _hex(chunk)
    _replace_child(device, device.find("PluginDesc"), new_pd)

    old_sc, new_sc = device.find("SourceContext"), template_vst3.find("SourceContext")
    if old_sc is not None and new_sc is not None:
        _replace_child(device, old_sc, copy.deepcopy(new_sc))

    mpe = device.find("MpePitchBendUsesTuning")
    if mpe is not None:
        mpe.set("Value", "true")
    return PortResult(label, True, f"VST2->VST3 device, {len(chunk)} bytes -> ProcessorState")


def port_au_uhe_amstate(src_patch: bytes, dst_dev: ET.Element, label: str) -> PortResult:
    """Replace a u-he AU's AM_STATE patch text with the old VST2 patch."""
    buf, plist = au_plist(dst_dev)
    if "AM_STATE" not in plist:
        return PortResult(label, False, "aupreset has no AM_STATE key")
    marker = src_patch.find(b"#AM=")
    if marker < 0:
        return PortResult(label, False, "source patch has no #AM= marker")
    plist["AM_STATE"] = src_patch[marker:]
    set_au_plist(buf, plist)
    return PortResult(label, True, f"AM_STATE {len(plist['AM_STATE'])} bytes")
