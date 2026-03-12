# How to Verify the Conversion Actually Worked

Running the script and seeing "Exported" does not mean the result is correct.
Here's how to actually check.

## Step 1: Read the Output

When you run the converter, it prints a summary. Look for these things:

### Good signs

```
INFO: Exported: output_glb_blender/example.glb
INFO:   Vertices: 539640
INFO:   Faces: 269820
INFO: Converted entity types:
INFO:   CIRCLE: 1312
INFO:   INSERT: 276
INFO:   LWPOLYLINE: 485
INFO:   POLYLINE: 1797
```

- Vertices and faces are non-zero (the file has actual geometry)
- No WARNING or ERROR lines
- The converted entity types match what you expect from the file

### Bad signs

```
WARNING: Skipped entity types:
WARNING:   3DSOLID: 2257
```

This means 2,257 solid shapes were dropped. The GLB will be missing geometry.

```
ERROR: No convertible geometry in example.dxf
ERROR: Failed to produce GLB for example.dxf
```

This means nothing was converted at all. No GLB was produced.

## Step 2: Run --analyze First

Before converting, check what's in the file:

```bash
python3 converters/blender/convert_blender.py --analyze output_dxf/file.dxf
```

This tells you:
- How many entities of each type are in the file
- Whether there are 3DSOLID entities (which will be skipped)
- Whether the file is 2D-only (which means the GLB will be flat/empty)

If the analysis shows only entity types that the converter handles (3DFACE,
MESH, LINE, POLYLINE, LWPOLYLINE, ARC, CIRCLE, SPLINE, INSERT), expect a
good conversion.

If it shows 3DSOLID as the main or only entity type, the conversion will
be partial or fail entirely.

## Step 3: Check the File Size

A GLB file with real geometry should have a reasonable file size:

- A simple model: 100KB - 5MB
- A complex building/structure: 5MB - 100MB
- A very detailed model: 100MB+

If the GLB is suspiciously tiny (under 10KB), something probably went wrong --
most of the geometry was likely skipped.

## Step 4: View the GLB

Open the GLB file and look at it. You can use:

- **BimPortVision** (the target viewer -- upload via DB)
- **https://gltf-viewer.donmccurdy.com/** (drag and drop the GLB file)
- **Blender** (File > Import > glTF)
- **VS Code** with the "glTF Tools" extension

Look for:
- Does the shape look right? Compare against the AutoCAD screenshot.
- Are there obvious holes or missing parts?
- Is the scale reasonable? (a building should look like a building, not a speck)
- Are colors roughly correct?

## Step 5: Compare Against AutoCAD

This is the most important check. Before converting, take a screenshot of
the file open in AutoCAD (or a DWG viewer). After converting, view the GLB
and compare side by side.

Things to look for:

| Check | What to compare |
|-------|----------------|
| Missing geometry | Are all shapes from AutoCAD present in the GLB? |
| Extra geometry | Is there anything in the GLB that shouldn't be there? |
| Scale | Does the model look the right size? |
| Position | Is the model centered or is it floating far from origin? |
| Colors | Do the colors roughly match? (exact match is not expected) |

## Step 6: Compare Against the Trimesh Converter

Run both converters on the same file and compare:

```bash
python3 converters/trimesh/convert.py output_dxf/file.dxf
python3 converters/blender/convert_blender.py output_dxf/file.dxf
```

Then compare:

```
output_glb/file.glb          <-- trimesh result
output_glb_blender/file.glb  <-- blender result
```

Open both in a viewer. If they look the same, the conversion is consistent.
If one looks better, use that one.

Things to compare:
- Vertex/face count (printed in the output)
- File size (ls -lh output_glb/*.glb output_glb_blender/*.glb)
- Visual appearance in a GLB viewer

## Quick Checklist

```
[ ] Ran --analyze, no unexpected issues
[ ] Conversion completed without ERROR lines
[ ] Vertex and face counts are non-zero
[ ] Skipped entity count is zero (or acceptably low)
[ ] GLB file size is reasonable (not suspiciously tiny)
[ ] GLB looks correct in a viewer
[ ] GLB matches the AutoCAD screenshot
[ ] Compared with trimesh converter output (if both available)
```

## What If Something Is Wrong

| Problem | Likely cause | Fix |
|---------|-------------|-----|
| GLB is empty or tiny | File is all 3DSOLID | Re-export DWG as STL, convert that instead |
| Missing shapes | 3DSOLID entities were skipped | Check WARNING output for skipped counts |
| Model is too small/big | Wrong unit scaling | Check the DXF units in the output, verify $INSUNITS |
| Model is off-center | Large survey coordinates | Should auto-center; if not, check the centroid values |
| Colors are wrong | ByLayer resolution failed | Compare with AutoCAD; color accuracy is approximate |
| Script crashes | Blender not found | Check ~/blender/blender exists; run blender --version |
| "No result from Blender" | Inner script error | Run with Blender directly to see the full error (see below) |

### Debugging: Run Blender Directly

If convert_blender.py gives you "No result from Blender script", run the
inner script manually to see the full error:

```bash
~/blender/blender --background --python converters/blender/_blender_convert_inner.py -- \
  '{"input": "output_dxf/file.dxf", "output": "output_glb_blender/file.glb", "analyze": false}'
```

This will show the full Blender output including Python tracebacks.
