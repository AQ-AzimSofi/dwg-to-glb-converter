"""
DXF/STL/OBJ to GLB converter using Blender.

Converts CAD files into GLB format using Blender's geometry engine.
This is an alternative to convert.py (ezdxf+trimesh) that leverages
Blender's built-in importers for potentially better results.

Supported input formats:
  - DXF (parsed via ezdxf, geometry built in Blender)
  - STL (Blender's native importer)
  - OBJ (Blender's native importer)

Usage:
    python3 convert_blender.py                              # convert all files in output_dxf/
    python3 convert_blender.py path/to/file.dxf             # convert a single file
    python3 convert_blender.py path/to/file.stl             # convert STL to GLB
    python3 convert_blender.py --analyze path/to/file.dxf   # analyze without converting

Requirements:
    Blender 4.2+ installed (with ezdxf in Blender's Python for DXF support)

This script is a wrapper that invokes Blender in background mode.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_INPUT_DIR = PROJECT_ROOT / "output_dxf"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output_glb_blender"
BLENDER_SCRIPT = SCRIPT_DIR / "_blender_convert_inner.py"

SUPPORTED_EXTENSIONS = {".dxf", ".stl", ".obj"}

BLENDER_PATH = Path.home() / "blender" / "blender"


def find_blender() -> str:
    if BLENDER_PATH.exists():
        return str(BLENDER_PATH)
    import shutil
    path = shutil.which("blender")
    if path:
        return path
    print("ERROR: Blender not found. Install Blender or set BLENDER_PATH.", file=sys.stderr)
    sys.exit(1)


def run_conversion(blender: str, input_path: Path, output_path: Path, analyze: bool = False) -> dict:
    args_data = {
        "input": str(input_path),
        "output": str(output_path),
        "analyze": analyze,
    }

    cmd = [
        blender,
        "--background",
        "--python", str(BLENDER_SCRIPT),
        "--",
        json.dumps(args_data),
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,
    )

    for line in result.stdout.splitlines():
        if line.startswith("RESULT:"):
            return json.loads(line[7:])
        if line.startswith("INFO:") or line.startswith("WARNING:") or line.startswith("ERROR:"):
            print(line)

    if result.returncode != 0:
        stderr_lines = [l for l in result.stderr.splitlines() if "Error" in l or "error" in l]
        for line in stderr_lines[:5]:
            print(f"ERROR: {line}", file=sys.stderr)
        return {"success": False, "error": "Blender process failed"}

    return {"success": False, "error": "No result from Blender script"}


def main():
    parser = argparse.ArgumentParser(
        description="Convert DXF/STL/OBJ files to GLB using Blender",
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="Path to a single file. If omitted, converts all supported files in output_dxf/",
    )
    parser.add_argument(
        "-o", "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory for GLB files",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Analyze DXF file without converting",
    )
    args = parser.parse_args()

    blender = find_blender()

    if args.input:
        input_files = [Path(args.input)]
    else:
        input_files = []
        for ext in SUPPORTED_EXTENSIONS:
            input_files.extend(DEFAULT_INPUT_DIR.rglob(f"*{ext}"))
        input_files.sort()
        if not input_files:
            print(f"ERROR: No supported files found in {DEFAULT_INPUT_DIR}", file=sys.stderr)
            sys.exit(1)
        print(f"INFO: Found {len(input_files)} file(s) in {DEFAULT_INPUT_DIR}")

    for filepath in input_files:
        if not filepath.exists():
            print(f"ERROR: File not found: {filepath}", file=sys.stderr)
            continue

        print(f"INFO: Processing: {filepath.name}")
        output_path = args.output_dir / (filepath.stem + ".glb")

        result = run_conversion(blender, filepath, output_path, args.analyze)

        if result.get("success"):
            print(f"INFO: Exported: {output_path}")
            print(f"INFO:   Vertices: {result.get('vertex_count', '?')}")
            print(f"INFO:   Faces: {result.get('face_count', '?')}")
        elif args.analyze and "entity_counts" in result:
            print(f"\n=== Analysis: {filepath.name} ===")
            print(f"Total entities: {result.get('total_entities', '?')}")
            for etype, count in sorted(result.get("entity_counts", {}).items()):
                print(f"  {etype}: {count}")
            if result.get("issues"):
                print("Issues:")
                for issue in result["issues"]:
                    print(f"  - {issue}")
            print()
        else:
            print(f"ERROR: Failed to produce GLB for {filepath.name}")
            if result.get("error"):
                print(f"ERROR: {result['error']}")

        if result.get("skipped"):
            print("WARNING: Skipped entity types:")
            for etype, count in sorted(result["skipped"].items()):
                print(f"WARNING:   {etype}: {count}")

        if result.get("converted"):
            print("INFO: Converted entity types:")
            for etype, count in sorted(result["converted"].items()):
                print(f"INFO:   {etype}: {count}")


if __name__ == "__main__":
    main()
