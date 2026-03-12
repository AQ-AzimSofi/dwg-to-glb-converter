"""
DWG/DXF to GLB converter using ODA (DWG->DXF 2010) + SAT text parsing.

The key insight: ODA FileConverter can output DXF in version 2010 format,
which stores 3DSOLID ACIS data as human-readable SAT text instead of
proprietary SAB binary. This SAT text can be parsed to extract B-rep
geometry (faces, edges, vertices) and tessellated into triangle meshes.

Pipeline:
  DWG -> ODA FileConverter -> DXF 2010 (SAT text) -> parse SAT -> tessellate -> GLB

Usage:
    python3 convert_freecad.py input_dwg/file.dwg
    python3 convert_freecad.py output_dxf/file.dxf        # already-converted DXF
    python3 convert_freecad.py --analyze input_dwg/file.dwg

Requirements:
    - ODA FileConverter (for DWG input)
    - ezdxf, trimesh, numpy (pip)
"""

import argparse
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

import ezdxf
import numpy as np
import trimesh

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output_glb_freecad"


def dwg_to_dxf_2010(dwg_path: Path, output_dir: Path) -> Path:
    """Convert DWG to DXF 2010 format using ODA FileConverter."""
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "xvfb-run", "-a", "ODAFileConverter",
        str(dwg_path.parent), str(output_dir),
        "ACAD2010", "DXF", "0", "1",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    dxf_path = output_dir / (dwg_path.stem + ".dxf")
    if not dxf_path.exists():
        raise RuntimeError(f"ODA conversion failed: {result.stderr}")
    return dxf_path


def ensure_dxf_2010(input_path: Path) -> Path:
    """Ensure we have a DXF 2010 file (SAT text for 3DSOLID)."""
    ext = input_path.suffix.lower()

    if ext == ".dwg":
        print(f"INFO: Converting DWG to DXF 2010 via ODA...")
        tmp_dir = Path(tempfile.mkdtemp(prefix="oda_dxf_"))
        return dwg_to_dxf_2010(input_path, tmp_dir)

    if ext == ".dxf":
        doc = ezdxf.readfile(str(input_path))
        has_sat = False
        has_sab = False
        for e in doc.modelspace():
            if e.dxftype() == "3DSOLID":
                has_sat = bool(e.sat)
                has_sab = bool(e.sab)
                break

        if has_sat:
            return input_path

        if has_sab:
            print(f"INFO: DXF has SAB binary, re-converting to DXF 2010 for SAT text...")
            tmp_dir = Path(tempfile.mkdtemp(prefix="oda_dxf_"))
            return dwg_to_dxf_2010(input_path, tmp_dir)

        return input_path

    raise ValueError(f"Unsupported format: {ext}")


# ---------------------------------------------------------------------------
# SAT text parser
# ---------------------------------------------------------------------------

class SatEntity:
    __slots__ = ("index", "type_name", "tokens")

    def __init__(self, index, type_name, tokens):
        self.index = index
        self.type_name = type_name
        self.tokens = tokens

    def __repr__(self):
        return f"SatEntity({self.index}, {self.type_name})"


def parse_sat_text(sat_lines: list[str]) -> list[SatEntity]:
    """Parse SAT text lines into entity objects."""
    entities = []
    idx = 0
    for line in sat_lines:
        line = line.strip()
        if not line or line.startswith("End-of-ACIS") or line.startswith("@") or line[0].isdigit():
            continue
        tokens = line.rstrip(" #").split()
        if not tokens:
            continue
        type_name = tokens[0]
        entities.append(SatEntity(idx, type_name, tokens))
        idx += 1
    return entities


def sat_ref(entities: list[SatEntity], token: str):
    """Resolve a $N reference to an entity, or None for $-1."""
    if token == "$-1":
        return None
    if token.startswith("$"):
        idx = int(token[1:])
        if 0 <= idx < len(entities):
            return entities[idx]
    return None


def _parse_refs(tokens: list[str]) -> list:
    """Extract all $N references from tokens in order, returning (index, token_pos) pairs."""
    refs = []
    for i, tok in enumerate(tokens):
        if tok.startswith("$"):
            if tok == "$-1":
                refs.append((-1, i))
            else:
                try:
                    refs.append((int(tok[1:]), i))
                except ValueError:
                    pass
    return refs


def _parse_floats(tokens: list[str]) -> list[float]:
    """Extract all float values from tokens in order."""
    result = []
    for tok in tokens:
        try:
            result.append(float(tok))
        except ValueError:
            pass
    return result


def get_point_coords(entities: list[SatEntity], point_entity: SatEntity) -> np.ndarray:
    """Extract xyz from a point entity: point $attr -1 $-1 x y z"""
    nums = _parse_floats(point_entity.tokens)
    if len(nums) >= 4:
        return np.array(nums[1:4])  # skip -1
    if len(nums) >= 3:
        return np.array(nums[-3:])
    return np.array([0, 0, 0])


def get_vertex_point(entities: list[SatEntity], vertex_entity: SatEntity) -> np.ndarray:
    """Get coordinates from a vertex: vertex $attr -1 $edge sense $point"""
    refs = _parse_refs(vertex_entity.tokens)
    for idx, _ in refs:
        if idx >= 0 and idx < len(entities) and entities[idx].type_name == "point":
            return get_point_coords(entities, entities[idx])
    return np.array([0, 0, 0])


def _get_coedge_fields(entities: list[SatEntity], coedge_ent: SatEntity):
    """Parse coedge: coedge $attr -1 $-1 $next $prev $partner $edge sense $loop $pcurve
    Note: ODA SAT has an extra $-1 field after attrib, shifting all indices by 1.
    Returns (next_coedge, edge, sense)."""
    refs = _parse_refs(coedge_ent.tokens)
    ref_indices = [idx for idx, _ in refs]

    # ref_indices: [0]=attr, [1]=extra_null, [2]=next, [3]=prev, [4]=partner, [5]=edge, [6]=loop, [7]=pcurve
    if len(ref_indices) < 6:
        return None, None, "forward"

    next_idx = ref_indices[2]
    edge_idx = ref_indices[5]

    next_ent = entities[next_idx] if 0 <= next_idx < len(entities) else None
    edge_ent = entities[edge_idx] if 0 <= edge_idx < len(entities) else None

    sense = "forward"
    for tok in coedge_ent.tokens:
        if tok in ("forward", "reversed"):
            sense = tok
            break

    return next_ent, edge_ent, sense


def _get_edge_vertices(entities: list[SatEntity], edge_ent: SatEntity):
    """Parse edge: edge $attr -1 $-1 $start_vert start_param $end_vert end_param $coedge $curve sense
    Returns (start_vertex, end_vertex, curve_entity)."""
    refs = _parse_refs(edge_ent.tokens)
    # refs: [0]=attr, [1]=extra_null, [2]=start_vert, [3]=end_vert, [4]=coedge, [5]=curve
    if len(refs) < 6:
        return None, None, None

    sv_idx = refs[2][0]
    ev_idx = refs[3][0]
    curve_idx = refs[5][0]

    sv = entities[sv_idx] if 0 <= sv_idx < len(entities) else None
    ev = entities[ev_idx] if 0 <= ev_idx < len(entities) else None
    curve = entities[curve_idx] if 0 <= curve_idx < len(entities) else None

    return sv, ev, curve


def trace_face_boundary(entities: list[SatEntity], face_ent: SatEntity) -> list[np.ndarray]:
    """Trace the outer boundary of a face by following coedge chain."""
    # face: $attr -1 $next_face $loop $shell $-1 $surface sense single
    refs = _parse_refs(face_ent.tokens)
    loop_ent = None
    for idx, _ in refs:
        if 0 <= idx < len(entities) and entities[idx].type_name == "loop":
            loop_ent = entities[idx]
            break

    if not loop_ent:
        return []

    # loop: $attr -1 $next_loop $coedge $face
    first_coedge = None
    for idx, _ in _parse_refs(loop_ent.tokens):
        if 0 <= idx < len(entities) and entities[idx].type_name == "coedge":
            first_coedge = entities[idx]
            break

    if not first_coedge:
        return []

    points = []
    visited = set()
    coedge = first_coedge

    while coedge and coedge.index not in visited:
        visited.add(coedge.index)

        next_ent, edge_ent, sense = _get_coedge_fields(entities, coedge)

        if edge_ent and edge_ent.type_name == "edge":
            sv, ev, curve = _get_edge_vertices(entities, edge_ent)
            # Use start or end vertex depending on coedge sense
            vert = sv if sense == "forward" else ev
            if vert and vert.type_name == "vertex":
                pt = get_vertex_point(entities, vert)
                if len(points) == 0 or not np.allclose(pt, points[-1], atol=1e-6):
                    points.append(pt)

        coedge = next_ent if (next_ent and next_ent.type_name == "coedge") else None

    return points


def _collect_all_face_vertices(entities: list[SatEntity], face_ent: SatEntity) -> list[np.ndarray]:
    """Collect vertex positions from ALL loops of a face."""
    points = []
    # Find all loops by following the loop chain
    refs = _parse_refs(face_ent.tokens)
    first_loop = None
    for idx, _ in refs:
        if 0 <= idx < len(entities) and entities[idx].type_name == "loop":
            first_loop = entities[idx]
            break

    if not first_loop:
        return points

    visited_loops = set()
    loop = first_loop
    while loop and loop.index not in visited_loops:
        visited_loops.add(loop.index)

        # Find the coedge in this loop
        loop_refs = _parse_refs(loop.tokens)
        first_coedge = None
        next_loop = None
        for idx, _ in loop_refs:
            if 0 <= idx < len(entities):
                if entities[idx].type_name == "coedge" and first_coedge is None:
                    first_coedge = entities[idx]
                elif entities[idx].type_name == "loop" and next_loop is None:
                    next_loop = entities[idx]

        # Traverse coedge chain
        if first_coedge:
            visited_coedges = set()
            coedge = first_coedge
            while coedge and coedge.index not in visited_coedges:
                visited_coedges.add(coedge.index)
                next_ent, edge_ent, sense = _get_coedge_fields(entities, coedge)
                if edge_ent and edge_ent.type_name == "edge":
                    sv, ev, curve = _get_edge_vertices(entities, edge_ent)
                    for v in (sv, ev):
                        if v and v.type_name == "vertex":
                            pt = get_vertex_point(entities, v)
                            points.append(pt)
                coedge = next_ent if (next_ent and next_ent.type_name == "coedge") else None

        loop = next_loop

    return points


def tessellate_cone_surface(entities: list[SatEntity], face_ent: SatEntity, n_segments=64) -> tuple:
    """Tessellate a cone/cylinder surface."""
    t = face_ent.tokens

    surface_ent = None
    for tok in t:
        ref = sat_ref(entities, tok)
        if ref and ref.type_name in ("cone-surface",):
            surface_ent = ref
            break

    if not surface_ent:
        return None, None

    # cone-surface $attr -1 $-1 cx cy cz ax ay az rx ry rz ratio ...
    st = surface_ent.tokens
    nums = _parse_floats(st)

    if len(nums) < 10:
        return None, None

    cx, cy, cz = nums[1], nums[2], nums[3]
    ax, ay, az = nums[4], nums[5], nums[6]
    rx, ry, rz = nums[7], nums[8], nums[9]

    radius = math.sqrt(rx*rx + ry*ry + rz*rz)

    center = np.array([cx, cy, cz])
    axis = np.array([ax, ay, az])
    axis_len = np.linalg.norm(axis)
    if axis_len < 1e-10:
        return None, None
    axis = axis / axis_len

    # Get height range from ALL vertices across all loops of this face
    all_verts = _collect_all_face_vertices(entities, face_ent)
    if not all_verts:
        return None, None

    projections = [np.dot(pt - center, axis) for pt in all_verts]
    z_min = min(projections)
    z_max = max(projections)

    if abs(z_max - z_min) < 1e-10:
        return None, None

    # Build cylinder/cone mesh
    if abs(axis[2]) < 0.9:
        perp1 = np.cross(axis, np.array([0, 0, 1]))
    else:
        perp1 = np.cross(axis, np.array([1, 0, 0]))
    perp1 = perp1 / np.linalg.norm(perp1)
    perp2 = np.cross(axis, perp1)

    vertices = []
    faces = []

    # Generate rings at z_min and z_max
    for z in [z_min, z_max]:
        ring_center = center + z * axis
        for i in range(n_segments):
            angle = 2 * math.pi * i / n_segments
            pt = ring_center + radius * (math.cos(angle) * perp1 + math.sin(angle) * perp2)
            vertices.append(pt)

    # Connect rings with triangles
    for i in range(n_segments):
        i0 = i
        i1 = (i + 1) % n_segments
        i2 = n_segments + i
        i3 = n_segments + (i + 1) % n_segments
        faces.append([i0, i2, i1])
        faces.append([i1, i2, i3])

    return np.array(vertices), np.array(faces)


def tessellate_plane_face(entities: list[SatEntity], face_ent: SatEntity) -> tuple:
    """Tessellate a planar face."""
    boundary = trace_face_boundary(entities, face_ent)
    if len(boundary) < 3:
        return None, None

    pts = np.array(boundary)

    # Project onto 2D for triangulation
    # Use the plane normal from the plane-surface entity
    t = face_ent.tokens
    surface_ent = None
    for tok in t:
        ref = sat_ref(entities, tok)
        if ref and ref.type_name == "plane-surface":
            surface_ent = ref
            break

    if surface_ent:
        st = surface_ent.tokens
        nums = []
        for tok in st:
            try:
                nums.append(float(tok))
            except ValueError:
                pass
        if len(nums) >= 9:
            normal = np.array([nums[4], nums[5], nums[6]])
        else:
            # compute from boundary
            v1 = pts[1] - pts[0]
            v2 = pts[2] - pts[0]
            normal = np.cross(v1, v2)
    else:
        v1 = pts[1] - pts[0]
        v2 = pts[2] - pts[0]
        normal = np.cross(v1, v2)

    norm_len = np.linalg.norm(normal)
    if norm_len < 1e-10:
        return None, None
    normal = normal / norm_len

    # Create projection axes
    if abs(normal[2]) < 0.9:
        u = np.cross(normal, np.array([0, 0, 1]))
    else:
        u = np.cross(normal, np.array([1, 0, 0]))
    u = u / np.linalg.norm(u)
    v = np.cross(normal, u)

    # Project to 2D
    center = pts.mean(axis=0)
    pts_2d = np.column_stack([
        np.dot(pts - center, u),
        np.dot(pts - center, v),
    ])

    # Ear-clipping triangulation
    tri_indices = ear_clip(pts_2d)
    if not tri_indices:
        return None, None

    return pts, np.array(tri_indices)


def ear_clip(pts_2d: np.ndarray) -> list[list[int]]:
    """Simple ear-clipping triangulation for a 2D polygon."""
    n = len(pts_2d)
    if n < 3:
        return []
    if n == 3:
        return [[0, 1, 2]]

    indices = list(range(n))
    triangles = []

    # Ensure CCW winding
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += pts_2d[i][0] * pts_2d[j][1]
        area -= pts_2d[j][0] * pts_2d[i][1]
    if area < 0:
        indices.reverse()

    max_iter = n * n
    iteration = 0
    while len(indices) > 2 and iteration < max_iter:
        iteration += 1
        ear_found = False
        for i in range(len(indices)):
            prev_i = (i - 1) % len(indices)
            next_i = (i + 1) % len(indices)
            a = pts_2d[indices[prev_i]]
            b = pts_2d[indices[i]]
            c = pts_2d[indices[next_i]]

            cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
            if cross <= 1e-12:
                continue

            is_ear = True
            for j in range(len(indices)):
                if j in (prev_i, i, next_i):
                    continue
                p = pts_2d[indices[j]]
                if point_in_triangle(p, a, b, c):
                    is_ear = False
                    break

            if is_ear:
                triangles.append([indices[prev_i], indices[i], indices[next_i]])
                indices.pop(i)
                ear_found = True
                break

        if not ear_found:
            for i in range(1, len(indices) - 1):
                triangles.append([indices[0], indices[i], indices[i + 1]])
            break

    return triangles


def point_in_triangle(p, a, b, c) -> bool:
    d1 = (p[0] - c[0]) * (a[1] - c[1]) - (a[0] - c[0]) * (p[1] - c[1])
    d2 = (p[0] - a[0]) * (b[1] - a[1]) - (b[0] - a[0]) * (p[1] - a[1])
    d3 = (p[0] - b[0]) * (c[1] - b[1]) - (c[0] - b[0]) * (p[1] - b[1])
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


def tessellate_ellipse_cap(entities: list[SatEntity], face_ent: SatEntity, n_segments=64) -> tuple:
    """Tessellate a circular/elliptical cap (plane face with ellipse boundary)."""
    t = face_ent.tokens

    # Find plane-surface for the Z level
    surface_ent = None
    for tok in t:
        ref = sat_ref(entities, tok)
        if ref and ref.type_name == "plane-surface":
            surface_ent = ref
            break

    # Find the loop -> coedge -> edge -> ellipse-curve
    loop_ent = None
    for tok in t:
        ref = sat_ref(entities, tok)
        if ref and ref.type_name == "loop":
            loop_ent = ref
            break

    if not loop_ent:
        return None, None

    # Find the coedge in the loop
    coedge_ent = None
    for tok in loop_ent.tokens:
        ref = sat_ref(entities, tok)
        if ref and ref.type_name == "coedge":
            coedge_ent = ref
            break

    if not coedge_ent:
        return None, None

    # Find the edge
    edge_ent = None
    for tok in coedge_ent.tokens:
        ref = sat_ref(entities, tok)
        if ref and ref.type_name == "edge":
            edge_ent = ref
            break

    if not edge_ent:
        return None, None

    # Find the ellipse-curve
    curve_ent = None
    for tok in edge_ent.tokens:
        ref = sat_ref(entities, tok)
        if ref and ref.type_name == "ellipse-curve":
            curve_ent = ref
            break

    if not curve_ent:
        return None, None

    # Parse ellipse-curve: $attr -1 $-1 cx cy cz ax ay az rx ry rz ratio ...
    ct = curve_ent.tokens
    nums = []
    for tok in ct:
        try:
            nums.append(float(tok))
        except ValueError:
            pass

    if len(nums) < 10:
        return None, None

    cx, cy, cz = nums[1], nums[2], nums[3]
    ax, ay, az = nums[4], nums[5], nums[6]
    rx, ry, rz = nums[7], nums[8], nums[9]

    center = np.array([cx, cy, cz])
    axis = np.array([ax, ay, az])
    axis_len = np.linalg.norm(axis)
    if axis_len < 1e-10:
        return None, None
    axis = axis / axis_len

    r_vec = np.array([rx, ry, rz])
    radius = np.linalg.norm(r_vec)
    if radius < 1e-10:
        return None, None

    # Create perpendicular vectors
    perp1 = r_vec / radius
    perp2 = np.cross(axis, perp1)

    # Generate fan triangulation
    vertices = [center]
    for i in range(n_segments):
        angle = 2 * math.pi * i / n_segments
        pt = center + radius * (math.cos(angle) * perp1 + math.sin(angle) * perp2)
        vertices.append(pt)

    faces = []
    for i in range(n_segments):
        i1 = i + 1
        i2 = (i + 1) % n_segments + 1
        faces.append([0, i1, i2])

    return np.array(vertices), np.array(faces)


def tessellate_solid_sat(sat_lines: list[str], transform_entity=None) -> trimesh.Trimesh:
    """Parse SAT text and tessellate all faces into a mesh."""
    entities = parse_sat_text(sat_lines)
    if not entities:
        return None

    all_vertices = []
    all_faces = []
    vertex_offset = 0

    # Find transform. Apply full transform (rotation + translation) so that
    # the solid is in its correct world position. The combined mesh will be
    # centered later.
    rotation = None
    translation = None
    for ent in entities:
        if ent.type_name == "transform":
            nums = []
            for tok in ent.tokens:
                try:
                    nums.append(float(tok))
                except ValueError:
                    pass
            if len(nums) >= 13:
                rotation = np.array(nums[1:10]).reshape(3, 3)
                translation = np.array([nums[10], nums[11], nums[12]])
            break

    for ent in entities:
        if ent.type_name != "face":
            continue

        verts = None
        tris = None

        # Determine surface type
        for tok in ent.tokens:
            ref = sat_ref(entities, tok)
            if ref is None:
                continue
            if ref.type_name == "cone-surface":
                verts, tris = tessellate_cone_surface(entities, ent)
                break
            elif ref.type_name == "plane-surface":
                boundary = trace_face_boundary(entities, ent)
                if len(boundary) >= 3:
                    verts, tris = tessellate_plane_face(entities, ent)
                else:
                    verts, tris = tessellate_ellipse_cap(entities, ent)
                break

        if verts is not None and tris is not None and len(verts) > 0 and len(tris) > 0:
            if rotation is not None and translation is not None:
                verts = (rotation @ verts.T).T + translation
            all_faces.append(tris + vertex_offset)
            all_vertices.append(verts)
            vertex_offset += len(verts)

    if not all_vertices:
        return None

    vertices = np.vstack(all_vertices)
    faces = np.vstack(all_faces)

    return trimesh.Trimesh(vertices=vertices, faces=faces, process=True)


# ---------------------------------------------------------------------------
# Main converter
# ---------------------------------------------------------------------------

def _cluster_by_proximity(points: np.ndarray, threshold: float) -> list[list[int]]:
    """Group point indices by spatial proximity (simple single-link clustering)."""
    n = len(points)
    visited = [False] * n
    groups = []

    for i in range(n):
        if visited[i]:
            continue
        group = []
        stack = [i]
        while stack:
            idx = stack.pop()
            if visited[idx]:
                continue
            visited[idx] = True
            group.append(idx)
            for j in range(n):
                if not visited[j]:
                    dist = np.linalg.norm(points[idx] - points[j])
                    if dist < threshold:
                        stack.append(j)
        groups.append(group)

    return groups


def convert_dxf_to_glb(dxf_path: Path, output_path: Path, analyze: bool = False) -> dict:
    """Convert a DXF file to GLB, handling 3DSOLID via SAT text parsing."""
    doc = ezdxf.readfile(str(dxf_path))
    msp = doc.modelspace()

    entity_counts = {}
    for e in msp:
        etype = e.dxftype()
        entity_counts[etype] = entity_counts.get(etype, 0) + 1

    if analyze:
        total = sum(entity_counts.values())
        solid_count = entity_counts.get("3DSOLID", 0)
        has_sat = False
        for e in msp:
            if e.dxftype() == "3DSOLID":
                has_sat = bool(e.sat)
                break
        return {
            "success": True,
            "entity_counts": entity_counts,
            "total_entities": total,
            "has_3dsolid": solid_count > 0,
            "solid_format": "SAT text" if has_sat else "SAB binary",
            "issues": [] if has_sat or solid_count == 0 else
                ["3DSOLID uses SAB binary - need DXF 2010 for SAT text"],
        }

    meshes = []
    converted = {}
    skipped = {}

    # Unit scaling
    units_var = doc.header.get("$INSUNITS", 0)
    unit_scale = {
        0: 1.0, 1: 0.0254, 2: 0.3048, 3: 1.609344e3,
        4: 0.001, 5: 0.01, 6: 1.0, 7: 1000.0,
        8: 0.0000254, 9: 0.0000254 * 12, 10: 0.9144,
        13: 1e-10, 14: 1e-7, 15: 1e-4,
    }.get(units_var, 1.0)

    for entity in msp:
        etype = entity.dxftype()

        if etype == "3DSOLID":
            sat = entity.sat
            if sat:
                mesh = tessellate_solid_sat(list(sat))
                if mesh and len(mesh.vertices) > 0:
                    meshes.append(mesh)
                    converted["3DSOLID"] = converted.get("3DSOLID", 0) + 1
                else:
                    skipped["3DSOLID (failed)"] = skipped.get("3DSOLID (failed)", 0) + 1
            else:
                skipped["3DSOLID (SAB)"] = skipped.get("3DSOLID (SAB)", 0) + 1

        elif etype == "3DFACE":
            pts = [
                np.array(entity.dxf.vtx0),
                np.array(entity.dxf.vtx1),
                np.array(entity.dxf.vtx2),
            ]
            if hasattr(entity.dxf, "vtx3"):
                vtx3 = np.array(entity.dxf.vtx3)
                if not np.allclose(vtx3, pts[2]):
                    pts.append(vtx3)

            verts = np.array(pts)
            if len(verts) == 3:
                faces = np.array([[0, 1, 2]])
            else:
                faces = np.array([[0, 1, 2], [0, 2, 3]])
            meshes.append(trimesh.Trimesh(vertices=verts, faces=faces))
            converted["3DFACE"] = converted.get("3DFACE", 0) + 1

        elif etype == "MESH":
            try:
                verts = np.array([list(v) for v in entity.vertices])
                raw_faces = list(entity.faces)
                tri_faces = []
                for f in raw_faces:
                    f = list(f)
                    if len(f) == 3:
                        tri_faces.append(f)
                    elif len(f) > 3:
                        for i in range(1, len(f) - 1):
                            tri_faces.append([f[0], f[i], f[i + 1]])
                if tri_faces:
                    meshes.append(trimesh.Trimesh(vertices=verts, faces=np.array(tri_faces)))
                    converted["MESH"] = converted.get("MESH", 0) + 1
            except Exception:
                skipped["MESH"] = skipped.get("MESH", 0) + 1

        elif etype in ("LINE", "POLYLINE", "LWPOLYLINE", "SPLINE", "ARC", "CIRCLE"):
            skipped[etype] = skipped.get(etype, 0) + 1

        elif etype == "INSERT":
            skipped["INSERT"] = skipped.get("INSERT", 0) + 1

    if not meshes:
        return {"success": False, "error": "No geometry produced", "converted": converted, "skipped": skipped}

    # Group meshes by proximity -- solids from a single structure should be
    # close together, but a DWG file may contain structures spread across km.
    centroids = np.array([m.vertices.mean(axis=0) for m in meshes])
    groups = _cluster_by_proximity(centroids, threshold=50.0)

    # Pick the best cluster: prefer compact groups with many meshes.
    # Score = mesh_count^2 / max_extent -- penalizes single-mesh clusters.
    best_group = max(groups, key=len)  # fallback: largest
    best_score = 0
    for g in groups:
        if len(g) < 2:
            continue
        pts = centroids[g]
        extent = max((pts.max(0) - pts.min(0)).max(), 1.0)
        score = (len(g) ** 2) / extent
        if score > best_score:
            best_score = score
            best_group = g

    selected = [meshes[i] for i in best_group]
    if len(groups) > 1:
        sel_pts = centroids[best_group]
        ext = sel_pts.max(0) - sel_pts.min(0) if len(best_group) > 1 else np.zeros(3)
        print(f"INFO: Found {len(groups)} spatial clusters, "
              f"selected densest ({len(selected)} meshes, "
              f"extents: {ext[0]:.1f} x {ext[1]:.1f} x {ext[2]:.1f})")

    combined = trimesh.util.concatenate(selected)

    verts = np.array(combined.vertices, copy=True)
    faces_arr = np.array(combined.faces, copy=True)

    if unit_scale != 1.0:
        verts *= unit_scale

    centroid = verts.mean(axis=0)
    verts -= centroid

    extents = verts.max(0) - verts.min(0)
    print(f"INFO: Model extents: {extents[0]:.2f} x {extents[1]:.2f} x {extents[2]:.2f}")

    centered = trimesh.Trimesh(vertices=verts, faces=faces_arr, process=False)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    centered.export(str(output_path), file_type="glb")

    return {
        "success": True,
        "vertex_count": len(centered.vertices),
        "face_count": len(centered.faces),
        "converted": converted,
        "skipped": skipped,
    }


def main():
    parser = argparse.ArgumentParser(description="DWG/DXF to GLB converter (SAT text parsing)")
    parser.add_argument("input", help="Path to DWG or DXF file")
    parser.add_argument("-o", "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--analyze", action="store_true")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    print(f"INFO: Processing: {input_path.name}")

    # Ensure DXF 2010 format (SAT text)
    dxf_path = ensure_dxf_2010(input_path)
    print(f"INFO: Using DXF: {dxf_path}")

    output_path = args.output_dir / (input_path.stem + ".glb")

    result = convert_dxf_to_glb(dxf_path, output_path, args.analyze)

    if args.analyze:
        print(f"\n=== Analysis: {input_path.name} ===")
        print(f"Total entities: {result.get('total_entities', '?')}")
        for etype, count in sorted(result.get("entity_counts", {}).items()):
            print(f"  {etype}: {count}")
        if result.get("has_3dsolid"):
            print(f"3DSOLID format: {result.get('solid_format', '?')}")
        if result.get("issues"):
            for issue in result["issues"]:
                print(f"  - {issue}")
    elif result.get("success"):
        print(f"INFO: Exported: {output_path}")
        print(f"INFO:   Vertices: {result.get('vertex_count', '?')}")
        print(f"INFO:   Faces: {result.get('face_count', '?')}")
    else:
        print(f"ERROR: {result.get('error', 'Unknown error')}")

    if result.get("converted"):
        print("INFO: Converted entity types:")
        for etype, count in sorted(result["converted"].items()):
            print(f"INFO:   {etype}: {count}")

    if result.get("skipped"):
        print("WARNING: Skipped entity types:")
        for etype, count in sorted(result["skipped"].items()):
            print(f"WARNING:   {etype}: {count}")


if __name__ == "__main__":
    main()
