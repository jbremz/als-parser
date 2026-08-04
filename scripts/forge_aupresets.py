#!/usr/bin/env python3
"""Forge .aupreset files from dead VST2 device states in Ableton projects.

Writes standard AU preset files into ~/Library/Audio/Presets/<Manu>/<Plugin>/,
where every AU host's preset menu (AU Lab, GarageBand, Logic, Live) picks them
up. This lets the user open an old plugin's exact project state in a
lightweight standalone host — no Ableton, no VST2 — since every plugin in
these projects ships an AU and AU Lab (x86_64) hosts even the Intel-only ones
in-process.

State-key layouts per vendor follow the recovery tooling's knowledge
(see .claude/skills/vst2-recovery). Reaktor's v5 VST2 chunks are written
verbatim into `vstdata`: the AU accepts and upgrades them (verified headless
via `audump --set`).
"""
import struct
import sys
import plistlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from als_parser.preset_port import load_als, vst2_chunk          # noqa: E402
from als_parser.device_templates import _device_name, _device_format  # noqa: E402

PRESET_ROOT = Path.home() / "Library/Audio/Presets"


def devices_named(als, plug):
    root = load_als(als).getroot()
    for t in root.iter():
        if t.tag not in ("AudioTrack", "MidiTrack", "GroupTrack",
                         "ReturnTrack", "MainTrack", "MasterTrack"):
            continue
        ne = t.find(".//Name/EffectiveName")
        tname = ne.get("Value") if ne is not None else t.tag
        for d in t.iter():
            if (d.tag == "PluginDevice" and _device_format(d) == "VST2"
                    and _device_name(d) == plug):
                yield tname, d


def forge(als, plug, *, manu_folder, plug_folder, type_, subtype, manu,
          state_key, transform=None, extra=None, label=None):
    made = []
    for tname, dev in devices_named(als, plug):
        chunk = vst2_chunk(dev)
        state = transform(chunk, dev) if transform else chunk
        if state is None:
            print(f"  !! {plug} on {tname}: transform refused, skipped")
            continue
        pl = {"name": f"{label or plug} - {tname}",
              "type": type_, "subtype": subtype, "manufacturer": manu,
              "version": 0, state_key: state}
        if extra:
            pl.update(extra(chunk, dev) or {})
        out_dir = PRESET_ROOT / manu_folder / plug_folder
        out_dir.mkdir(parents=True, exist_ok=True)
        proj = Path(als).stem.replace(" [recovered]", "")
        base = f"{proj} - {tname}".replace("/", "_")
        f = out_dir / f"{base}.aupreset"
        n = 2
        while f in made:   # same track name twice in one project (e.g. 2x on one track)
            f = out_dir / f"{base} ({n}).aupreset"
            n += 1
        with open(f, "wb") as fh:
            plistlib.dump(pl, fh)
        made.append(f)
        print(f"  aupreset: {f.relative_to(PRESET_ROOT)}")
    return made


def cc(s):
    return struct.unpack(">I", s.encode())[0]


if __name__ == "__main__":
    AG = "/Users/JBremner/Music/Music Projects/Ableton/ag bed flip Project/ag bed flip n.als"
    MO = "/Users/JBremner/Music/Music Projects/Ableton/Moombah Squeak Project/Moombah Squeak Melodic ha nn.als"
    AS_ = "/Users/JBremner/Music/Music Projects/Ableton/chorus house hats smack Project [astroblaster]/astroblaster cutdown.als"
    NU = "/Users/JBremner/Music/Music Projects/Ableton/no usb man Project [bidi bidi bah]/no usb man.als"

    def izotope_rewrap(chunk, dev):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from als_parser.recover import _izotope_rewrap
        # template shape: any modern iZotope stream (magic+ver+sizes+zlib)
        fake_tpl = struct.pack("<IIII", 0x688ADE, 3, 4, 0) + b"\x78\x9c"
        return _izotope_rewrap(chunk, fake_tpl)

    def program_of(chunk, dev):
        e = dev.find(".//VstPreset/ProgramNumber")
        return {"mCurPreset": int(e.get("Value"))} if e is not None else {}

    print("== SubBoomBass v1 (bank + active preset) ==")
    forge(AG, "SubBoomBass", manu_folder="Rob Papen", plug_folder="SubBoomBass",
          type_=cc("aumu"), subtype=cc("PBss"), manu=cc("RPCX"),
          state_key="mCompleteData", extra=program_of,
          label="seq Tomzer 3 Sign (edited)")

    print("== Reaktor 6 (v5 chunks — the AU upgrades them on load) ==")
    for als in (MO, NU):
        forge(als, "Reaktor 6", manu_folder="Native Instruments",
              plug_folder="Reaktor 6", type_=cc("aumu"), subtype=cc("NiR6"),
              manu=cc("-NI-"), state_key="vstdata")

    print("== Roth-AIR (JUCE state) ==")
    for als in (AG, MO, AS_):
        forge(als, "Roth-AIR", manu_folder="Rothmann", plug_folder="Roth-AIR",
              type_=cc("aufx"), subtype=cc("J6vc"), manu=cc("Roth"),
              state_key="jucePluginState")

    print("== Ozone 9 Elements (zlib rewrap) ==")
    for als in (AG, MO):
        forge(als, "Ozone 9 Elements", manu_folder="iZotope",
              plug_folder="Ozone 9 Elements", type_=cc("aufx"),
              subtype=cc("ZnE9"), manu=cc("iZtp"),
              state_key="data", transform=izotope_rewrap)

    print("== iZotope DDLY (strip 4B prefix) ==")
    forge(MO, "iZotope DDLY Dynamic Delay", manu_folder="iZotope",
          plug_folder="iZotope DDLY Dynamic Delay", type_=cc("aufx"),
          subtype=cc("iZDD"), manu=cc("iZtp"),
          state_key="data", transform=lambda c, d: c[4:])
