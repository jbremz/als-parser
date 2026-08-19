---
name: vst2-recovery
description: Recover dead VST2 plugin presets in an Ableton project into VST3/AU replacements, producing a new working .als. Use when the user has an Ableton project with missing/unavailable VST2 plugins (macOS dropped VST2), gives a project path and optionally a screenshot of Ableton's missing-plugin warning, and wants the plugins swapped for installed VST3/AU equivalents with their presets carried over.
---

# VST2 preset recovery for Ableton projects

The preset state of a dead VST2 plugin is still stored verbatim inside the
`.als` (gzipped XML). This skill drives `scripts/recover_vst2.py` +
`src/als_parser/{recover,preset_port,track_ops,device_templates}.py` to swap
dead devices for installed VST3/AU replacements and port the state across.
All heavy lifting is in code; your job is the glue: identify what's broken,
build the spec, run, verify, and report what needs manual attention.

## Workflow

### 1. Identify the affected plugins

Inputs: a project `.als` path, and usually a screenshot of Ableton's
missing-plugin warning (status bar / browser "missing" section).

- If given a screenshot, read the plugin names from it. Names there may be
  truncated or styled — treat them as hints, not ground truth.
- The authoritative inventory always comes from:

  ```bash
  python scripts/recover_vst2.py analyze "path/to/Project.als"
  ```

  This lists every VST2 plugin, per-plugin track counts, which replacement
  formats are installed (VST3 preferred, AU fallback), and writes a starter
  `recover.spec.json` next to the project.
- Cross-check screenshot names against the analyze output; flag anything in
  the screenshot that analyze didn't find (might be a missing *sample* or an
  AU, not a VST2).

### 2. Build the spec

Edit `recover.spec.json`. Each entry:

```json
{"vst2_name": "Transient Master", "target_fmt": "VST3"}
```

- Same plugin, VST3 and AU both installed → prefer VST3 (analyze already does).
- **Renamed/variant replacement** (e.g. `++pitchsift` → `+pitchsift`): add
  `"target_name"` and, for AU targets, a `"param_map"` of
  `dest-param → src-param`. Build it by comparing parameter names:

  ```python
  from als_parser.preset_port import load_als, param_order
  from als_parser.device_templates import find_device_node
  root = load_als("Project.als").getroot()
  print(param_order(find_device_node(root, "OldPlug", "VST2")))
  # harvest/instantiate the target once, then compare its param_order
  ```

  Map only confident matches; dropped params are lossy — tell the user which.
- No replacement installed → leave it out of the spec and report it.

### 3. Dry-run, then apply

```bash
python scripts/recover_vst2.py recover "path/to/Project.als"            # dry run
python scripts/recover_vst2.py recover "path/to/Project.als" --apply    # write
```

- Default mode (`inplace`) converts devices on their own tracks and writes a
  **new file** `Project [recovered].als` — the original is never touched.
- `--mode duplicate` instead makes muted `- COMPAT` copies of affected tracks
  in the same file (safety backup made) — use when the user wants originals
  side-by-side for A/B.
- Templates are harvested from the project, then the surrounding Ableton
  library (cached in `~/.als_recover_cache`). "NO TEMPLATE" → ask the user to
  drop that plugin once onto any track of any project, save, re-run.

### 4. Read the report honestly

Per device: `ported` (state carried over), or `skipped` with a reason + a
`recall:` hint (ensemble/preset filenames found in the chunk). Skipped
devices keep their dead VST2 — no data is ever destroyed. Summarise for the
user: what's fully recovered, what's partial (param_map drops), what needs
manual recall and exactly which file/preset to load (e.g. Reaktor: load the
named `.ens`, exact knob state is locked in NI's binary).

### 5. Verify

- The tool already validates: XML re-parses, fresh pointee ids collision-free,
  no dangling `PointeeId`, and **no sibling-id collisions** — Ableton refuses
  to load a file where two children of the same parent share an `Id`
  (tag-agnostic; error: "Non-unique list ids"). Any spliced node must get an
  Id unique among ALL its siblings (`_unique_sibling_id`), not just its tag.
  If you touched library code, also re-check byte equality of a ported chunk
  vs its source.
- **Era mixing corrupts files**: templates harvested from pre-Live-11 projects
  carry old-style FileRefs (`RelativePath` with `RelativePathElement` children,
  no Value attribute) inside LastPresetRef/PresetRef; Live 12 refuses the file
  ("Required attribute 'Value' missing"). Grafted nodes are sanitised
  (`_strip_preset_refs` — these are cosmetic browser pointers) and the writer
  gates on the document's `RelativePathElement` count not growing. ET can't
  catch this — it's schema-valid XML, wrong era.
- Ask the user to open the result in Ableton. Ground truth after they save:
  every replacement device should show `IsPlaceholderDevice=false` (and AU:
  `IsUnusable=false`) in the re-saved file.

## How the state porting works (for debugging new plugins)

| Target | State container | Method |
|---|---|---|
| VST3 | `<Vst3Preset><ProcessorState>` (hex) | Graft template's `PluginDesc` onto the existing device wrapper, then write the VST2 chunk at the alignment `_vst3_align` finds (see below). Wrapper keeps `ParameterList`/`AutomationTarget`s → automation survives. |
| AU (soundhack-style) | `.aupreset` plist in `<AuPreset><Buffer>`, key `vstdata` = VST2 FXP | Rewrite the FXP float array (floats = normalised param values, in `ParameterList` order) from the old device's `Manual` values. |
| AU (u-he-style) | plist key `AM_STATE` = text patch | Copy the old chunk's text from `#AM=` onward — identical across formats. |
| AU (JUCE-style) | plist key `jucePluginState` | The VST2 chunk is wrapper-independent — copy it in whole. Usually a `VC2!`+size+XML blob (`copyXmlToBinary`: Hysteresis, Roth-AIR, Spaceship Delay), but some plugins return raw bytes (Klevgrand Gaffel: bare float dump) — the port checks src and template agree on which class they are. |
| AU (Soundtoys) | plist key `soundtoys-data` (a plist *string*) | Same `WIDGET = ...;` text as the VST2 chunk; normalise `\r` line endings to `\n` and strip trailing NULs. |
| AU (Rob Papen) | plist key `mCompleteData` (+ `mCurPreset`) | The whole VST2 bank blob verbatim; set `mCurPreset` from the Ableton device's `VstPreset/ProgramNumber`. |
| AU (FPCh-style, e.g. KV331 SynthMasterCM) | plist key `vstdata` = `CcnK/FPCh` | An opaque-chunk VST2 preset container; rebuild the 60-byte header around the raw VST2 chunk (`_rebuild_fpch`). |
| AU (iZotope-style: `data` is the only state key) | standard `data` key | Same stream the plugin's VST3 uses — the VST3 alignment rules run against the template's default data (Mobius/DDLY: strip 4-byte prefix; Ozone: zlib rewrap, below). |
| AU (unknown layout) | — | Skipped with a report. To support a new vendor: dump the plist keys and find where state lives; add a branch in `recover._port_au`. |

iZotope Ozone-era state is a zlib'd JSON "Context State" dict; VST2 wraps it in
an old 20-byte header, AU/VST3 in `<magic 0x688ADE><ver 3><zlib_len+4><raw_len>`
— `_izotope_rewrap` rebuilds the modern wrapper around the VST2 chunk's own
payload, so "containers genuinely differ" turned out portable after all.

**Architecture / activation lessons (Apple Silicon):**
- **Check `xattr` quarantine flags before declaring an AU dead or hung**: old
  Safari-downloaded components carry `com.apple.quarantine`, and Gatekeeper
  blocks them *silently* headless (instantiate -1, indefinite hangs) and with
  dialog storms in GUI hosts. Clearing the flag revived three "dead" plugins.
- Live hosts all Intel AUs in ONE Rosetta bridge service: a single crashing
  plugin ("lost connection to the Audio Unit" on every bridged AU at the same
  timestamp) takes the whole family down. Read the AUHostingServiceXPC crash
  report in ~/Library/Logs/DiagnosticReports to find the one culprit — e.g.
  BassStation aborting in CCPreference::ReadFromStore because of a stale
  32-bit-era pref plist (1-byte <data> values); retiring the pref file fixed
  it. Sandboxed test processes may not reproduce (different pref containers).
- Don't trust per-plugin error messages emitted DURING a bridge crash storm:
  "audio buffer size could not be set" (KV331 SynthMasterCM) looked like a
  per-plugin incompatibility but was collateral of the shared service dying —
  the same AU inserted fresh works fine. Diagnose the crasher first, retest
  the others after it's gone.
- Audit target-plugin arch (`file` on the bundle binary) BEFORE choosing a
  format: Intel-only VST3s (iZotope Mobius/DDLY) are simply invisible to
  native Live — the port "works" but the device can never load. Intel-only
  **AUs** DO load, via macOS's out-of-process Rosetta bridge (sandbox-safe
  ones, at least — old non-sandbox-safe AUs like SubBoomBass v1 don't bridge).
  Preference: arm64 VST3 > bridged AU > skip-with-Rosetta-note.
- Replacement devices must inherit the ORIGINAL device's On switch
  (`_carry_on_state`) — template donors may have been bypassed in their source
  project, which shipped ported devices switched off.
- u-he VST2 param names carry a product prefix ("Tyrell: Tune2"); the AU
  exposes the bare name — the automation relinker strips the prefix.

VST3 bonus rule — **`VstW` wrapper**: some VST3s (ValhallaRoom) store their
state as Steinberg's VST2-compat container `VstW`(16B) + `CcnK/FBCh` (bank,
160B header) or `/FPCh` + the raw VST2 chunk. If a harvested VST3 template
starts `VstW`, port by rebuilding that wrapper around the old chunk
(`_wrap_vstw`) — class-checked against the template's inner chunk.

## No template anywhere? Synthesize the device node

When a plugin was never used as AU/VST3 in any saved project, you can build the
AU device node without the user touching Ableton:

1. Component identity codes are in
   `/Library/Audio/Plug-Ins/Components/<X>.component/Contents/Info.plist`
   (`AudioComponents` → type/subtype/manufacturer 4-char codes;
   `struct.unpack(">I", code.encode())` gives the ints Ableton stores).
   JUCE plugins reuse the VST2 UniqueId as the AU subtype — a good cross-check.
2. Get the default `.aupreset` dict by instantiating the AU headless with
   `tools/audump.c` (`clang -framework AudioToolbox -framework CoreFoundation
   -o audump tools/audump.c`). It also prints the parameter table (JUCE AU
   param ids are string *hashes*, not indices — never guess them). Some AUs
   hang without a GUI session (Roth-AIR, Spaceship Delay did) — kill after
   ~45s and fall back to a minimal dict in the vendor's known key pattern,
   flagging the port as lower-confidence in your report.
3. `device_templates.synthesize_au_device(donor, ...)` grafts the identity +
   preset onto a real same-vendor device node and blanks the parameter list.
   Write the result into the harvest cache (`~/.als_recover_cache/<norm>__AU.xml`)
   and the normal `recover` run picks it up.
4. **On Apple Silicon, "dead" usually means "Intel-only", not dead.** Before
   declaring a plugin unrecoverable, check the binary arch (`file`) and
   re-validate under Rosetta: `arch -x86_64 auval -v TYPE SUBT MANU`.
   (SubBoomBass v1 "FATAL error" natively — full PASS under x86_64.) The
   bit-exact recovery fallback for any x86_64 VST2: set Ableton Live to
   "Open using Rosetta" (Get Info), open the ORIGINAL project — VST2 support
   comes back and every device loads its exact state — freeze/flatten the
   affected tracks, save-as, return to native Live. This is why originals
   must never be modified: they are the Rosetta-recoverable ground truth.
   No app duplication needed — Live is universal, so
   `arch -x86_64 ".../Ableton Live 12 Suite.app/Contents/MacOS/Live"` launches
   it under Rosetta directly (double-clickable "Open Live 12 ROSETTA/NATIVE.command"
   launchers live in the user's Ableton folder — they also swap per-arch
   snapshots of Live's plugin DB (Live Database/Live-plugins-*.db +
   PluginScanner.txt/AddOns.txt in the versioned prefs dir), so each mode
   scans once ever instead of on every switch). Bonus: universal
   replacement plugins also load under Rosetta, so old and new can run
   side-by-side in one session for manual matching.
5. Before injecting a legacy/cross-version state into a plugin, **pre-screen
   headless** with `audump ... out.plist --set crafted.plist` — it sets
   ClassInfo before dumping, so the plugin's own validation runs without a
   DAW. (SubBoomBass 2 printed "Fatal error, preset Size wrong!" and reverted
   to defaults when fed a v1 bank — proving v1→v2 injection is impossible
   before anyone wasted money or an Ableton session on it.) Successor-version
   plugins usually can NOT read predecessor state; verify, never assume. A
   dead v1 plugin's presets can still be exported as .fxp/.fxb (forge the
   FxCk/FxBk container) for use in any working v1 instance elsewhere.

Cross-format sanity check that unlocked Gaffel: its AU's default
`jucePluginState` was 36 bytes — the exact size of the VST2 chunk — proving the
state is the same raw blob on both sides.

`_vst3_align` chunk-compat rules (vendor-verified): `VC2!` magic both sides →
JUCE, port verbatim (soothe2); identical first-6-bytes → port verbatim (NI
Transient Master ✓, Reaktor CSAR v5≠v6 correctly rejected); bytes 4–16 equal →
size-prefixed both sides, port verbatim (Waves `TAPS`); VST2[4:12] or [8:16] ==
VST3[0:8] → strip the prefix (iZotope DDLY). iZotope Ozone containers genuinely
differ (VST2 and VST3 wrappers unrelated) → honest skip, manual recall.

For AU replacements, automation envelopes pointing at the old device are
relinked by matching the On switch and parameter names (exact, via `param_map`,
or by prefix — VST2 truncates parameter names to 15 chars). Unmatched refs are
reported for manual relinking.

## Hard-won gotchas

- Devices live on **Group, Return and Main/Master tracks** too (sends, master
  chains). `MasterTrack` is the pre-Live-12 tag; Live 12 says `MainTrack`. The
  tool scans all of them; if you write ad-hoc scans, don't stop at Audio/Midi.
- Ableton's missing-plugin warning under-reports — always trust `analyze` over
  the screenshot (Moombah: screenshots showed 10 plugins, the file had 16).
- **Waves** plugins live inside `WaveShell*.vst3/.component`, so the installed-
  format check can't match them by name ("installed: NO" is cosmetic) — the
  harvested template carries the correct shell identity and works regardless.
- If no template exists anywhere in the library for a plugin (user never used
  its AU/VST3 in a saved project), have the user make one throwaway set with
  each missing plugin instantiated once, save it, and re-run — the harvester
  picks them up from that file.
- When synthesizing a soundhack-style (`vstdata` FxCk) template, the param
  table must be written into **PluginFloatParameter entries only** — the FXP
  port maps name→slot via `param_order()`, which reads float entries; filling
  mixed float/enum donor entries misaligns the mapping (symptom: "FXP: N
  params" with N < the plugin's param count).
- The installed-plugin name can differ from the VST2 name in surprising ways:
  CumulusVST → Loomer "Cumulus", DeClick2 → "Acon Digital DeClick 2",
  RP-Distort → RP-Distort_64.component. When `analyze` says "no replacement
  found", check the AU registry before believing it — enumerate ALL registered
  AUs (with codes) via a tiny AudioComponentFindNext loop rather than
  `auval -a`, which can hang for 10+ minutes.
- Instruments (`aumu`) need an instrument donor node for synthesis (e.g. a
  u-he BazilleCM device), not an effect node.
- Ableton's `Log.txt` (~/Library/Preferences/Ableton/Live x/) is the ground
  truth for "components errored on open" reports: dead VST2s show as
  "VST2: Restore N failed: <name>"; missing samples and AU failures are
  logged distinctly.

- **Strip whitespace before unhexlifying** any Ableton hex buffer — Live 12
  wraps long `ProcessorState`/`Buffer` text with newlines/tabs.
- Only pointee-space defs (`AutomationTarget`, `ModulationTarget`, `Pointee`,
  `*ModulationTarget`, `ControllerTargets.N`) get fresh ids when splicing
  subtrees; only `PointeeId` elements reference them. Local ids (devices,
  clips, params) must be left alone. Use `track_ops.remap_pointee_ids`.
- The chunk/plist is the authoritative state Ableton restores — the mirrored
  `ParameterList` values are cosmetic.
- ElementTree round-trips `.als` files losslessly (only cosmetic quote-style
  changes on attributes containing `"`); always work on the parsed tree, never
  regex-edit the XML.
- `if element:` is False for childless ET elements — always `is not None`.
- Ableton may rename the project folder on save; don't cache absolute paths.
- Old projects (Live 10) upgrade fine when Live 12 opens the recovered file,
  but tell the user to open + save + spot-check rather than assuming.
