# How the Blender Converter Works

## The Big Picture

This converter takes CAD files (DXF, STL, OBJ) and turns them into GLB files
that can be viewed in BimPortVision. It uses Blender as the engine to do this.

There are two scripts that work together:

- `convert_blender.py` -- the script you run. It handles arguments and launches Blender.
- `_blender_convert_inner.py` -- runs inside Blender. Does the actual conversion.

You never run `_blender_convert_inner.py` directly. `convert_blender.py` does it for you.

## What Happens When You Run It

```
You run convert_blender.py
  |
  v
It finds Blender on your system (~/blender/blender)
  |
  v
It launches Blender in the background (no GUI, headless)
  |
  v
Blender runs _blender_convert_inner.py internally
  |
  v
That script reads your file, builds 3D geometry, and exports as GLB
  |
  v
The result (vertex count, face count, success/fail) is printed back
  |
  v
convert_blender.py reads that result and shows it to you
```

## How Each File Type Is Handled

### STL and OBJ (the simple ones)

Blender has built-in importers for these. The script just does:

1. Import the file using Blender's native importer
2. If the model has huge coordinates (common in survey data), center it
3. Export as GLB

That's it. These formats are already triangle meshes so there's nothing
complicated to figure out.

### DXF (the complex one)

Blender 4.2 does not have a built-in DXF importer, so the script uses
`ezdxf` (a Python library) to read the DXF file and then creates Blender
geometry manually for each entity type:

| Entity | What it is | What the script does |
|--------|-----------|---------------------|
| 3DFACE | A triangle or quad | Creates it directly as a mesh face |
| MESH | A mesh with vertices and faces | Extracts vertices/faces, triangulates polygons |
| LINE | A single line segment | Turns it into a thin flat strip (ribbon) so it's visible |
| POLYLINE | Connected line segments in 3D | Same as LINE but for each segment |
| LWPOLYLINE | Connected line segments in 2D | Same, uses the elevation value for Z |
| SPLINE | A curve | Connects control points as ribbons |
| ARC | Part of a circle | Breaks it into small straight segments, then ribbons |
| CIRCLE | A full circle | Same as ARC but goes all the way around (64 segments) |
| INSERT | A reference to a reusable block | Looks up the block, processes its contents with the right position/rotation/scale |
| 3DSOLID | A solid 3D shape (ACIS format) | **Skipped** -- cannot be read from DXF |

After all entities are processed:

1. Unit scaling is applied (mm, cm, inches, etc. are all converted to meters)
2. All the individual mesh objects are joined into one combined mesh
3. If coordinates are very large, the model is centered
4. The combined mesh is exported as GLB

### Colors

Each entity in a DXF file can have a color. The script resolves colors in
this order:

1. If the entity has a TrueColor value (24-bit RGB), use that
2. If it has an ACI (AutoCAD Color Index) number, look up the RGB value
3. If the color is "ByLayer", look up the layer's color
4. If nothing works, default to light gray

Colors are applied as Blender materials on the mesh.

### The Ribbon Trick

GLB files only support triangles -- they have no concept of "lines". So
lines, polylines, arcs, and circles can't be exported as-is. The script
turns each line segment into a thin flat rectangle (two triangles) that's
0.01 units wide. This makes them visible in the GLB viewer.

## What It Cannot Do

- **3DSOLID entities**: These use a proprietary format (ACIS) that is locked
  inside the DXF file. Neither Blender nor ezdxf can read them. If your file
  is mostly 3DSOLID, you need to re-export the original DWG as STL instead.
- **Text, dimensions, hatches**: These are annotation/2D elements that don't
  translate to 3D mesh data. They are skipped.
- **Materials and textures**: DWG materials are Autodesk-specific. Only basic
  colors are preserved.

## vs. the Trimesh Converter

The trimesh converter (`converters/trimesh/convert.py`) does the same DXF
parsing with the same entity support. The key differences:

| | Trimesh | Blender |
|---|---------|---------|
| DXF parsing | ezdxf | ezdxf (same) |
| Mesh building | trimesh (Python) | Blender (C/C++ engine) |
| GLB export | trimesh | Blender's glTF exporter |
| 3DSOLID | Optional acis_tessellator | Skipped |
| File size | Larger GLB output | Smaller GLB output |
| Dependencies | pip install ezdxf trimesh numpy | Blender 4.2+ (~500MB) |
| STL/OBJ | trimesh loader | Blender native importers |

Blender produces smaller GLB files because its exporter is more optimized
(mesh compression, deduplication). But the trimesh converter has an optional
`acis_tessellator.py` module that can handle some 3DSOLID entities, which
the Blender version cannot.
