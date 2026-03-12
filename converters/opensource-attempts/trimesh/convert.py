"""
DXF/STL/OBJ to GLB converter.

Converts CAD files into GLB format for use in BimPortVision.

Supported input formats:
  - DXF (handles 3DFACE, MESH, LINE, ARC, CIRCLE, INSERT, etc.)
  - STL (use this when DXF files contain only 3DSOLID entities)
  - OBJ (Wavefront OBJ)

Usage:
    python3 convert.py                              # convert all files in output_dxf/
    python3 convert.py path/to/file.dxf             # convert a single file
    python3 convert.py path/to/file.stl             # convert STL to GLB
    python3 convert.py --analyze path/to/file.dxf   # analyze without converting

For files with 3DSOLID entities:
    ODA File Converter cannot tessellate 3DSOLID (proprietary ACIS format)
    when exporting to DXF. Instead, re-export from ODA as STL, or from
    AutoCAD using STLOUT / EXPORT commands, then convert the STL here.

Requirements:
    pip install ezdxf trimesh numpy
"""

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import trimesh

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_INPUT_DIR = PROJECT_ROOT / "output_dxf"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output_glb"

SUPPORTED_EXTENSIONS = {".dxf", ".stl", ".obj"}

# AutoCAD Color Index (ACI) to RGB mapping for the standard 10 colors.
ACI_COLORS = {
    1: (255, 0, 0),
    2: (255, 255, 0),
    3: (0, 255, 0),
    4: (0, 255, 255),
    5: (0, 0, 255),
    6: (255, 0, 255),
    7: (255, 255, 255),
    8: (128, 128, 128),
    9: (192, 192, 192),
}

# DXF $INSUNITS values to meters
UNIT_TO_METERS = {
    0: 1.0,
    1: 0.0254,     # inches
    2: 0.3048,     # feet
    3: 1609.344,   # miles
    4: 0.001,      # mm
    5: 0.01,       # cm
    6: 1.0,        # meters
    7: 1000.0,     # km
    8: 0.0000254,  # microinches
    9: 0.001,      # mils
    10: 0.9144,    # yards
    11: 1.0e-10,   # angstroms
    12: 1.0e-9,    # nanometers
    13: 1.0e-6,    # microns
    14: 0.01,      # decimeters
    15: 10.0,      # decameters
    16: 100.0,     # hectometers
    17: 1.0e15,    # gigameters
    18: 1.496e11,  # AU
    19: 9.461e15,  # light years
    20: 3.086e16,  # parsecs
}

logger = logging.getLogger("dxf2glb")


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
    )


def get_scale_factor(doc) -> float:
    try:
        units = doc.header.get("$INSUNITS", 0)
    except Exception:
        units = 0
    factor = UNIT_TO_METERS.get(units, 1.0)
    if units == 0:
        logger.warning("No $INSUNITS set in DXF. Assuming meters.")
    else:
        logger.info(f"DXF units: {units} (scale to meters: {factor})")
    return factor


def resolve_color(entity, doc) -> tuple[int, int, int, int]:
    color_index = entity.dxf.get("color", 256)

    if color_index == 256:
        layer_name = entity.dxf.get("layer", "0")
        try:
            layer = doc.layers.get(layer_name)
            color_index = layer.color
        except Exception:
            color_index = 7

    if color_index == 0:
        color_index = 7

    if entity.dxf.hasattr("true_color"):
        tc = entity.dxf.true_color
        r = (tc >> 16) & 0xFF
        g = (tc >> 8) & 0xFF
        b = tc & 0xFF
        return (r, g, b, 255)

    try:
        from ezdxf.colors import aci2rgb
        rgb = aci2rgb(color_index)
        return (rgb[0], rgb[1], rgb[2], 255)
    except (ImportError, IndexError):
        pass

    rgb = ACI_COLORS.get(color_index, (200, 200, 200))
    return (rgb[0], rgb[1], rgb[2], 255)


def analyze_dxf(filepath: Path) -> dict:
    import ezdxf
    doc = ezdxf.readfile(str(filepath))
    msp = doc.modelspace()

    entity_counts: dict[str, int] = {}
    for entity in msp:
        etype = entity.dxftype()
        entity_counts[etype] = entity_counts.get(etype, 0) + 1

    issues = []

    solid_count = entity_counts.get("3DSOLID", 0)
    if solid_count > 0:
        convertible_types = {"3DFACE", "MESH", "POLYFACE", "POLYMESH",
                             "LINE", "LWPOLYLINE", "POLYLINE", "ARC",
                             "CIRCLE", "SPLINE", "INSERT"}
        has_convertible = any(t in entity_counts for t in convertible_types)

        if not has_convertible:
            issues.append(
                f"File contains ONLY 3DSOLID entities ({solid_count} total). "
                "These use proprietary ACIS format and cannot be converted from DXF. "
                "SOLUTION: Re-export from ODA File Converter as STL instead of DXF, "
                "or use AutoCAD's STLOUT command, then run this script on the STL file."
            )
        else:
            issues.append(
                f"Found {solid_count} 3DSOLID entities (will be skipped). "
                "Other geometry types are present and will be converted."
            )

    mesh_types = {"3DFACE", "MESH", "POLYFACE", "POLYMESH"}
    has_3d = any(t in entity_counts for t in mesh_types)
    if not has_3d and solid_count == 0:
        line_types = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "SPLINE"}
        has_2d = any(t in entity_counts for t in line_types)
        if has_2d:
            issues.append(
                "File appears to be 2D only (no 3D mesh entities found). "
                "GLB output may be flat or empty."
            )

    if entity_counts.get("INSERT", 0) > 0:
        issues.append(
            f"Found {entity_counts['INSERT']} INSERT (block reference) entities. "
            "These will be exploded during conversion."
        )

    scale = get_scale_factor(doc)

    return {
        "entity_counts": entity_counts,
        "issues": issues,
        "scale_factor": scale,
        "total_entities": sum(entity_counts.values()),
    }


def triangulate_3dface(points: list) -> list[list]:
    if len(points) == 3:
        return [points]
    if len(points) == 4:
        return [
            [points[0], points[1], points[2]],
            [points[0], points[2], points[3]],
        ]
    return []


def tessellate_arc(center, radius, start_angle, end_angle, segments=32):
    if end_angle < start_angle:
        end_angle += 360.0

    angles = np.linspace(
        np.radians(start_angle), np.radians(end_angle), segments + 1
    )
    cx, cy = center[0], center[1]
    cz = center[2] if len(center) > 2 else 0.0

    return [
        (cx + radius * np.cos(a), cy + radius * np.sin(a), cz)
        for a in angles
    ]


def line_to_ribbon(p1, p2, width=0.01):
    p1 = np.array(p1, dtype=float)
    p2 = np.array(p2, dtype=float)
    direction = p2 - p1
    length = np.linalg.norm(direction)
    if length < 1e-10:
        return [], []

    direction /= length

    up = np.array([0, 0, 1], dtype=float)
    if abs(np.dot(direction, up)) > 0.99:
        up = np.array([0, 1, 0], dtype=float)
    perp = np.cross(direction, up)
    perp_norm = np.linalg.norm(perp)
    if perp_norm < 1e-10:
        return [], []
    perp = perp / perp_norm * width * 0.5

    v0 = p1 - perp
    v1 = p1 + perp
    v2 = p2 + perp
    v3 = p2 - perp

    vertices = [v0.tolist(), v1.tolist(), v2.tolist(), v3.tolist()]
    faces = [[0, 1, 2], [0, 2, 3]]
    return vertices, faces


def convert_stl_to_glb(filepath: Path, output_path: Path) -> dict:
    """Convert an STL file to GLB. STL is already tessellated mesh data."""
    mesh = trimesh.load(str(filepath), file_type="stl")

    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate(
            [g for g in mesh.geometry.values() if isinstance(g, trimesh.Trimesh)]
        )

    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        logger.error(f"No geometry in STL file: {filepath.name}")
        return {"success": False, "vertex_count": 0, "face_count": 0}

    centroid = mesh.centroid
    if np.any(np.abs(centroid) > 10000):
        logger.info(
            f"Large coordinates detected (centroid: {centroid}). "
            "Centering model."
        )
        mesh.apply_translation(-centroid)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    scene = trimesh.Scene(geometry={"model": mesh})
    scene.export(str(output_path), file_type="glb")

    logger.info(f"Exported: {output_path}")
    logger.info(f"  Vertices: {len(mesh.vertices)}")
    logger.info(f"  Faces: {len(mesh.faces)}")

    return {
        "success": True,
        "vertex_count": len(mesh.vertices),
        "face_count": len(mesh.faces),
        "output": str(output_path),
    }


def convert_obj_to_glb(filepath: Path, output_path: Path) -> dict:
    """Convert an OBJ file to GLB."""
    mesh = trimesh.load(str(filepath), file_type="obj")

    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate(
            [g for g in mesh.geometry.values() if isinstance(g, trimesh.Trimesh)]
        )

    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        logger.error(f"No geometry in OBJ file: {filepath.name}")
        return {"success": False, "vertex_count": 0, "face_count": 0}

    centroid = mesh.centroid
    if np.any(np.abs(centroid) > 10000):
        logger.info(
            f"Large coordinates detected (centroid: {centroid}). "
            "Centering model."
        )
        mesh.apply_translation(-centroid)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    scene = trimesh.Scene(geometry={"model": mesh})
    scene.export(str(output_path), file_type="glb")

    logger.info(f"Exported: {output_path}")
    logger.info(f"  Vertices: {len(mesh.vertices)}")
    logger.info(f"  Faces: {len(mesh.faces)}")

    return {
        "success": True,
        "vertex_count": len(mesh.vertices),
        "face_count": len(mesh.faces),
        "output": str(output_path),
    }


def convert_dxf_to_glb(
    filepath: Path,
    output_path: Path,
    ribbon_width: float = 0.01,
) -> dict:
    import ezdxf
    doc = ezdxf.readfile(str(filepath))
    msp = doc.modelspace()
    scale = get_scale_factor(doc)

    all_meshes = []
    skipped: dict[str, int] = {}
    converted: dict[str, int] = {}

    def record_converted(etype: str, count: int = 1):
        converted[etype] = converted.get(etype, 0) + count

    def record_skipped(etype: str, count: int = 1):
        skipped[etype] = skipped.get(etype, 0) + count

    def process_entity(entity, transform=None):
        etype = entity.dxftype()
        color = resolve_color(entity, doc)

        if etype == "3DFACE":
            points = [
                (entity.dxf.vtx0[0], entity.dxf.vtx0[1], entity.dxf.vtx0[2]),
                (entity.dxf.vtx1[0], entity.dxf.vtx1[1], entity.dxf.vtx1[2]),
                (entity.dxf.vtx2[0], entity.dxf.vtx2[1], entity.dxf.vtx2[2]),
            ]
            if entity.dxf.vtx3 != entity.dxf.vtx2:
                points.append(
                    (entity.dxf.vtx3[0], entity.dxf.vtx3[1], entity.dxf.vtx3[2])
                )
            triangles = triangulate_3dface(points)
            for tri in triangles:
                verts = np.array(tri, dtype=np.float64)
                if transform is not None:
                    verts = (transform @ np.hstack(
                        [verts, np.ones((len(verts), 1))]
                    ).T).T[:, :3]
                faces = np.array([[0, 1, 2]], dtype=np.int64)
                mesh = trimesh.Trimesh(vertices=verts, faces=faces)
                mesh.visual.face_colors = [color]
                all_meshes.append(mesh)
            record_converted(etype)

        elif etype == "MESH":
            try:
                vertices = [list(v) for v in entity.vertices]
                faces_raw = list(entity.faces)
                verts = np.array(vertices, dtype=np.float64)
                if transform is not None:
                    verts = (transform @ np.hstack(
                        [verts, np.ones((len(verts), 1))]
                    ).T).T[:, :3]
                tri_faces = []
                for face in faces_raw:
                    idxs = list(face)
                    if len(idxs) == 3:
                        tri_faces.append(idxs)
                    elif len(idxs) == 4:
                        tri_faces.append([idxs[0], idxs[1], idxs[2]])
                        tri_faces.append([idxs[0], idxs[2], idxs[3]])
                    elif len(idxs) > 4:
                        for i in range(1, len(idxs) - 1):
                            tri_faces.append([idxs[0], idxs[i], idxs[i + 1]])
                if tri_faces:
                    mesh = trimesh.Trimesh(
                        vertices=verts,
                        faces=np.array(tri_faces, dtype=np.int64),
                    )
                    mesh.visual.face_colors = [color] * len(tri_faces)
                    all_meshes.append(mesh)
                record_converted(etype)
            except Exception as e:
                logger.debug(f"Failed to convert MESH: {e}")
                record_skipped(etype)

        elif etype in ("LINE", "LWPOLYLINE", "POLYLINE", "SPLINE"):
            points = []
            try:
                if etype == "LINE":
                    start = entity.dxf.start
                    end = entity.dxf.end
                    points = [
                        (start[0], start[1], start[2] if len(start) > 2 else 0),
                        (end[0], end[1], end[2] if len(end) > 2 else 0),
                    ]
                elif etype == "LWPOLYLINE":
                    for x, y, *rest in entity.get_points(format="xyb"):
                        points.append((x, y, entity.dxf.get("elevation", 0)))
                    if entity.closed and len(points) > 1:
                        points.append(points[0])
                elif etype == "POLYLINE":
                    for v in entity.vertices:
                        loc = v.dxf.location
                        points.append((loc[0], loc[1], loc[2]))
                    if entity.is_closed:
                        points.append(points[0])
                elif etype == "SPLINE":
                    ctrl = list(entity.control_points)
                    if ctrl:
                        points = [(p[0], p[1], p[2] if len(p) > 2 else 0) for p in ctrl]
            except Exception as e:
                logger.debug(f"Failed to extract points from {etype}: {e}")
                record_skipped(etype)
                return

            if len(points) >= 2:
                for i in range(len(points) - 1):
                    verts, faces = line_to_ribbon(points[i], points[i + 1], ribbon_width)
                    if verts:
                        v = np.array(verts, dtype=np.float64)
                        if transform is not None:
                            v = (transform @ np.hstack(
                                [v, np.ones((len(v), 1))]
                            ).T).T[:, :3]
                        mesh = trimesh.Trimesh(
                            vertices=v,
                            faces=np.array(faces, dtype=np.int64),
                        )
                        mesh.visual.face_colors = [color] * len(faces)
                        all_meshes.append(mesh)
                record_converted(etype)
            else:
                record_skipped(etype)

        elif etype in ("ARC", "CIRCLE"):
            try:
                center = entity.dxf.center
                radius = entity.dxf.radius
                ctr = (center[0], center[1], center[2] if len(center) > 2 else 0)
                if etype == "CIRCLE":
                    pts = tessellate_arc(ctr, radius, 0, 360, segments=64)
                else:
                    pts = tessellate_arc(
                        ctr, radius,
                        entity.dxf.start_angle,
                        entity.dxf.end_angle,
                        segments=32,
                    )
                for i in range(len(pts) - 1):
                    verts, faces = line_to_ribbon(pts[i], pts[i + 1], ribbon_width)
                    if verts:
                        v = np.array(verts, dtype=np.float64)
                        if transform is not None:
                            v = (transform @ np.hstack(
                                [v, np.ones((len(v), 1))]
                            ).T).T[:, :3]
                        mesh = trimesh.Trimesh(
                            vertices=v,
                            faces=np.array(faces, dtype=np.int64),
                        )
                        mesh.visual.face_colors = [color] * len(faces)
                        all_meshes.append(mesh)
                record_converted(etype)
            except Exception as e:
                logger.debug(f"Failed to convert {etype}: {e}")
                record_skipped(etype)

        elif etype == "INSERT":
            block_name = entity.dxf.name
            try:
                block = doc.blocks.get(block_name)
            except Exception:
                logger.debug(f"Block '{block_name}' not found")
                record_skipped(etype)
                return

            insert_point = entity.dxf.get("insert", (0, 0, 0))
            x_scale = entity.dxf.get("xscale", 1.0)
            y_scale = entity.dxf.get("yscale", 1.0)
            z_scale = entity.dxf.get("zscale", 1.0)
            rotation = np.radians(entity.dxf.get("rotation", 0.0))

            cos_r = np.cos(rotation)
            sin_r = np.sin(rotation)
            block_transform = np.array([
                [x_scale * cos_r, -y_scale * sin_r, 0, insert_point[0]],
                [x_scale * sin_r,  y_scale * cos_r, 0, insert_point[1]],
                [0,                0,                z_scale, insert_point[2] if len(insert_point) > 2 else 0],
                [0,                0,                0, 1],
            ], dtype=np.float64)

            if transform is not None:
                block_transform = transform @ block_transform

            for sub_entity in block:
                process_entity(sub_entity, block_transform)
            record_converted(etype)

        elif etype == "3DSOLID":
            try:
                from acis_tessellator import tessellate_solid
                sab_data = entity.acis_data
                if sab_data:
                    mesh = tessellate_solid(sab_data)
                    if mesh is not None and len(mesh.faces) > 0:
                        mesh.visual.face_colors = [color] * len(mesh.faces)
                        all_meshes.append(mesh)
                        record_converted(etype)
                        return
            except Exception as e:
                logger.debug(f"3DSOLID tessellation failed: {e}")
            record_skipped(etype)

        else:
            record_skipped(etype)

    for entity in msp:
        process_entity(entity)

    if skipped.get("3DSOLID", 0) > 0:
        logger.warning(
            f"Skipped {skipped['3DSOLID']} 3DSOLID entities (proprietary ACIS format). "
            "To convert these, re-export the DWG as STL using ODA or AutoCAD, "
            "then run: python3 convert.py path/to/file.stl"
        )

    if not all_meshes:
        logger.error(f"No convertible geometry in {filepath.name}")
        return {
            "success": False,
            "converted": converted,
            "skipped": skipped,
            "mesh_count": 0,
        }

    combined = trimesh.util.concatenate(all_meshes)

    if scale != 1.0:
        combined.apply_scale(scale)

    centroid = combined.centroid
    if np.any(np.abs(centroid) > 10000):
        logger.info(
            f"Large coordinates detected (centroid: {centroid}). "
            "Centering model."
        )
        combined.apply_translation(-centroid)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    scene = trimesh.Scene(geometry={"model": combined})
    scene.export(str(output_path), file_type="glb")

    logger.info(f"Exported: {output_path}")
    logger.info(f"  Vertices: {len(combined.vertices)}")
    logger.info(f"  Faces: {len(combined.faces)}")

    return {
        "success": True,
        "converted": converted,
        "skipped": skipped,
        "vertex_count": len(combined.vertices),
        "face_count": len(combined.faces),
        "output": str(output_path),
    }


def convert_file(filepath: Path, output_path: Path, ribbon_width: float = 0.01) -> dict:
    ext = filepath.suffix.lower()
    if ext == ".dxf":
        return convert_dxf_to_glb(filepath, output_path, ribbon_width)
    elif ext == ".stl":
        return convert_stl_to_glb(filepath, output_path)
    elif ext == ".obj":
        return convert_obj_to_glb(filepath, output_path)
    else:
        logger.error(f"Unsupported format: {ext}")
        return {"success": False}


def main():
    parser = argparse.ArgumentParser(
        description="Convert DXF/STL/OBJ files to GLB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "For DXF files containing only 3DSOLID entities:\n"
            "  3DSOLID uses proprietary ACIS format that cannot be read from DXF.\n"
            "  Re-export the original DWG as STL (via ODA or AutoCAD STLOUT),\n"
            "  then convert the STL with this script.\n"
        ),
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
    parser.add_argument(
        "--ribbon-width",
        type=float,
        default=0.01,
        help="Width of line ribbons in model units (default: 0.01)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.input:
        input_files = [Path(args.input)]
    else:
        input_files = []
        for ext in SUPPORTED_EXTENSIONS:
            input_files.extend(DEFAULT_INPUT_DIR.rglob(f"*{ext}"))
        input_files.sort()
        if not input_files:
            logger.error(f"No supported files found in {DEFAULT_INPUT_DIR}")
            sys.exit(1)
        logger.info(f"Found {len(input_files)} file(s) in {DEFAULT_INPUT_DIR}")

    for filepath in input_files:
        if not filepath.exists():
            logger.error(f"File not found: {filepath}")
            continue

        logger.info(f"Processing: {filepath.name}")

        if args.analyze:
            if filepath.suffix.lower() != ".dxf":
                logger.info(f"  Analysis only available for DXF files, skipping {filepath.name}")
                continue
            result = analyze_dxf(filepath)
            print(f"\n=== Analysis: {filepath.name} ===")
            print(f"Total entities: {result['total_entities']}")
            print(f"Scale factor: {result['scale_factor']}")
            print("\nEntity types:")
            for etype, count in sorted(result["entity_counts"].items()):
                print(f"  {etype}: {count}")
            if result["issues"]:
                print("\nIssues:")
                for issue in result["issues"]:
                    print(f"  - {issue}")
            else:
                print("\nNo obvious issues detected.")
            print()
            continue

        output_path = args.output_dir / (filepath.stem + ".glb")
        result = convert_file(filepath, output_path, args.ribbon_width)

        if result.get("skipped"):
            logger.warning("Skipped entity types:")
            for etype, count in sorted(result["skipped"].items()):
                logger.warning(f"  {etype}: {count}")

        if result.get("converted"):
            logger.info("Converted entity types:")
            for etype, count in sorted(result["converted"].items()):
                logger.info(f"  {etype}: {count}")

        if not result.get("success"):
            logger.error(f"Failed to produce GLB for {filepath.name}")


if __name__ == "__main__":
    main()
