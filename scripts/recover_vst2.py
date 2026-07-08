#!/usr/bin/env python3
"""Recover dead VST2 presets in an Ableton project into VST3/AU replacements.

Workflow
--------
1. See what's affected and get a starter spec:

       python scripts/recover_vst2.py analyze "MyProject/Song.als"

   Prints every VST2 plugin, how many tracks use it, which replacement formats
   are installed, and writes a starter `recover.spec.json` next to the project.

2. Edit the spec (choose VST3 vs AU per plugin; add target_name / param_map for
   renamed plugins), then dry-run:

       python scripts/recover_vst2.py recover "MyProject/Song.als"

3. Apply. Default mode converts devices in place and writes a NEW file
   ("Song [recovered].als") — the original is never touched:

       python scripts/recover_vst2.py recover "MyProject/Song.als" --apply

   Or keep the originals visible side-by-side with muted "- COMPAT" track
   copies, written back into the same file (safety backup made):

       python scripts/recover_vst2.py recover "MyProject/Song.als" --apply --mode duplicate

Templates are harvested from the project first, then the surrounding Ableton
library (auto-detected, cached under ~/.als_recover_cache). Anything that can't
be ported confidently (incompatible chunk, missing template) is reported, never
silently broken. A safety copy of the .als is made before writing.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from als_parser.recover import analyze_project, recover_project, PluginSpec  # noqa: E402

CACHE = Path.home() / ".als_recover_cache"


def _ableton_root(als: Path) -> Path:
    """Walk up to the 'Ableton' library dir if we're inside one, else parent."""
    for p in als.resolve().parents:
        if p.name.lower() == "ableton":
            return p
    return als.resolve().parent


def _library_als(root: Path, limit=4000) -> list:
    return [p for p in root.rglob("*.als") if "/Backup/" not in str(p)][:limit]


def cmd_analyze(args):
    als = Path(args.project)
    suggested = analyze_project(als)
    spec_path = als.with_name("recover.spec.json")
    spec = {"plugins": [{"vst2_name": s.vst2_name, "target_fmt": s.target_fmt}
                        for s in suggested]}
    spec_path.write_text(json.dumps(spec, indent=2))
    print(f"\nStarter spec written: {spec_path}")
    print("Edit it (VST3/AU per plugin; add target_name/param_map for renamed "
          "plugins), then run: recover")


def cmd_recover(args):
    als = Path(args.project)
    spec_path = Path(args.spec) if args.spec else als.with_name("recover.spec.json")
    if not spec_path.exists():
        sys.exit(f"No spec at {spec_path}. Run 'analyze' first.")
    spec = json.loads(spec_path.read_text())
    specs = [PluginSpec(**p) for p in spec["plugins"]]

    lib = Path(spec["library"]) if spec.get("library") else _ableton_root(als)
    library_paths = _library_als(lib)
    print(f"Harvesting templates from {len(library_paths)} project(s) under {lib}\n")

    recover_project(als, specs, library_paths=library_paths,
                    cache_dir=CACHE, apply=args.apply,
                    mode=args.mode, output=args.output)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="list dead VST2 + installed replacements")
    a.add_argument("project")
    a.set_defaults(func=cmd_analyze)
    r = sub.add_parser("recover", help="duplicate tracks, swap plugins, port state")
    r.add_argument("project")
    r.add_argument("--spec", help="path to spec json (default: recover.spec.json next to project)")
    r.add_argument("--apply", action="store_true", help="write the file (default: dry run)")
    r.add_argument("--mode", choices=["inplace", "duplicate"], default="inplace",
                   help="inplace: convert devices on their tracks, write a NEW "
                        "'[recovered]' file (default). duplicate: muted '- COMPAT' "
                        "track copies written back to the same file.")
    r.add_argument("--output", help="output path for inplace mode "
                                    "(default: '<name> [recovered].als')")
    r.set_defaults(func=cmd_recover)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
