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
  no dangling `PointeeId`. If you touched library code, also re-check byte
  equality of a ported chunk vs its source.
- Ask the user to open the result in Ableton. Ground truth after they save:
  every replacement device should show `IsPlaceholderDevice=false` (and AU:
  `IsUnusable=false`) in the re-saved file.

## How the state porting works (for debugging new plugins)

| Target | State container | Method |
|---|---|---|
| VST3 | `<Vst3Preset><ProcessorState>` (hex) | Graft template's `PluginDesc` onto the existing device wrapper, then write the VST2 chunk at the alignment `_vst3_align` finds (see below). Wrapper keeps `ParameterList`/`AutomationTarget`s → automation survives. |
| AU (soundhack-style) | `.aupreset` plist in `<AuPreset><Buffer>`, key `vstdata` = VST2 FXP | Rewrite the FXP float array (floats = normalised param values, in `ParameterList` order) from the old device's `Manual` values. |
| AU (u-he-style) | plist key `AM_STATE` = text patch | Copy the old chunk's text from `#AM=` onward — identical across formats. |
| AU (JUCE-style) | plist key `jucePluginState` | The VST2 chunk (JUCE `VC2!` + size + XML, from `copyXmlToBinary`) is wrapper-independent — copy it in whole. (Glitchmachines Hysteresis; any JUCE plugin.) |
| AU (unknown layout) | — | Skipped with a report. To support a new vendor: dump the plist keys and find where state lives; add a branch in `recover._port_au`. |

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
