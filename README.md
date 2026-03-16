# DWG to GLB Converter

Convert AutoCAD DWG files to colored GLB (binary glTF) for Matterport.

## Why Autodesk's Cloud?

DWG files containing 3DSOLID entities store geometry in the proprietary ACIS format. No open-source tool can tessellate ACIS data into triangle meshes. Autodesk's cloud APIs are the only programmatic path that works, because their server-side engine includes the ACIS kernel.

See [report_dwg_to_glb.md](report_dwg_to_glb.md) for a detailed comparison of all methods tested.

## Conversion Pipelines

### 1. GUI Application (Recommended)

Tkinter-based GUI that auto-detects file type and color data, then runs the appropriate pipeline.

```bash
python3 gui.py
```

### 2. Design Automation FBX (Cloud, Preferred)

Uses 3ds Max in Autodesk's cloud to convert DWG to FBX with materials, then locally to GLB.

**Pipeline:** DWG -> Design Automation (3ds Max + MAXScript) -> FBX with materials -> pyassimp + trimesh -> GLB

```bash
# One-time setup
python3 converters/design-automation-fbx/setup_da_fbx.py

# Convert
python3 converters/design-automation-fbx/convert_da_fbx.py input_dwg/file.dwg [-o output_dir]
```

### 3. Local FBX + DWG (No Cloud Needed)

Customer exports FBX from 3ds Max/AutoCAD (grey geometry), our script reads colors from the original DWG and combines them.

**Pipeline:** FBX (geometry) + DWG/DXF (layer colors via ezdxf) -> pyassimp + trimesh -> colored GLB

```bash
python3 converters/fbx-dwg-to-glb/convert.py <fbx_file> <dwg_or_dxf_file> [-o output_dir]
```

### 4. Model Derivative API (Legacy)

Uses APS Model Derivative API to translate DWG to SVF, then extracts GLB locally.

**Pipeline:** DWG -> Model Derivative API -> SVF -> forge-convert-utils -> glTF -> GLB

```bash
python3 converters/aps/convert_aps.py input_dwg/file.dwg
node converters/aps/svf_to_glb.js <urn> [output_dir]
```

### 5. Design Automation STL (Legacy)

Runs a custom AutoCAD C# plugin in the cloud that exports entities to STL.

**Pipeline:** DWG -> Design Automation (AutoCAD + C# plugin) -> STL -> trimesh -> GLB

```bash
# One-time setup (requires .NET SDK)
python3 converters/design-automation/setup_da.py

# Convert
python3 converters/design-automation/convert_da.py input_dwg/file.dwg [-o output_dir]
```

## Setup

### APS Credentials

Create an Autodesk developer account and an APS application with **Data Management API**, **Model Derivative API**, and **Design Automation API** enabled.

```bash
cp .env.example .env
# Edit .env with your APS_CLIENT_ID and APS_CLIENT_SECRET
```

### Python Dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests python-dotenv trimesh numpy ezdxf pyassimp setuptools
```

### Node.js Dependencies (Legacy Method 4 Only)

```bash
npm install
```

## Project Structure

```
gui.py                              -- Tkinter GUI application
converters/
  design-automation-fbx/            -- Method 2: DA 3ds Max FBX (preferred cloud)
    convert_da_fbx.py               -- Full pipeline: upload, convert, download, GLB
    setup_da_fbx.py                 -- One-time APS activity registration
    export_fbx.ms                   -- MAXScript: wirecolor -> material, export FBX
  fbx-dwg-to-glb/                   -- Method 3: Local FBX + DWG
    convert.py                      -- Combine grey FBX geometry + DWG layer colors
  aps/                              -- Method 4: APS SVF (legacy)
    convert_aps.py
    svf_to_glb.js
  design-automation/                -- Method 5: DA AutoCAD STL (legacy)
    convert_da.py
    setup_da.py
    plugin/                         -- C# AutoCAD plugin
  opensource-attempts/              -- Archived failed approaches
docs/
  process-explained.md              -- Technical explanation of APS API workflow
report_dwg_to_glb.md               -- Method comparison report
```

## Requirements

- Python 3.12+
- Node.js 18+ (legacy Method 4 only)
- .NET SDK (legacy Method 5 plugin build only)
- Autodesk developer account (free tier available)
- ODAFileConverter (for DWG -> DXF conversion in Method 3)
