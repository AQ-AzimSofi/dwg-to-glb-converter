# DWG to GLB Converter

Convert AutoCAD DWG files to GLB (binary glTF) using Autodesk cloud APIs.

## Why Autodesk's Cloud?

DWG files containing 3DSOLID entities store geometry in the proprietary ACIS format. No open-source tool can tessellate ACIS data into triangle meshes. Autodesk's cloud APIs are the only programmatic path that works, because their server-side engine includes the ACIS kernel.

See [report_dwg_to_glb.md](report_dwg_to_glb.md) for a detailed comparison of all methods tested.

## Two Conversion Approaches

### 1. Model Derivative API (SVF path)

Uses the APS Model Derivative API to translate DWG to SVF, then extracts GLB locally.

**Pipeline:** DWG -> upload -> Model Derivative API -> SVF -> forge-convert-utils -> glTF -> gltf-pipeline -> GLB

**Pros:** Simple setup, no custom plugin needed.
**Cons:** Requires Node.js for SVF extraction, multi-step local post-processing.

### 2. Design Automation API (STL path)

Runs a custom AutoCAD plugin in the cloud that exports 3DSOLID entities directly to STL, then converts to GLB locally with trimesh.

**Pipeline:** DWG -> upload -> Design Automation (AutoCAD + custom plugin) -> STL -> trimesh -> GLB

**Pros:** Single Python script for the full pipeline, handles all entity types (3DSOLID, 3DFACE, MESH, curves).
**Cons:** Requires one-time setup to register the plugin with APS.

## Setup

### APS credentials

Create an Autodesk developer account and an APS application with **Data Management API**, **Model Derivative API**, and **Design Automation API** enabled.

```bash
cp .env.example .env
# Edit .env with your APS_CLIENT_ID and APS_CLIENT_SECRET
```

### Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests python-dotenv trimesh
```

### Node.js dependencies (only needed for Method 1)

```bash
npm install
```

### Design Automation setup (only needed for Method 2)

One-time registration of the AutoCAD plugin with APS. Requires .NET SDK for building the C# plugin.

```bash
python3 converters/design-automation/setup_da.py
```

## Usage

### Method 1: Model Derivative API

#### Step 1: Upload DWG and translate to SVF

```bash
python3 converters/aps/convert_aps.py input_dwg/your_file.dwg
```

This uploads the DWG to Autodesk's cloud and translates it to SVF format. It outputs a base64 URN.

#### Step 2: Extract GLB from SVF

```bash
node converters/aps/svf_to_glb.js <urn> [output_dir]
```

#### Step 3: Package as GLB (if needed)

```bash
npx gltf-pipeline -i output_glb_aps/output.gltf -o output_glb_aps/output.glb
```

For complex models with validation errors:

```bash
npx @gltf-transform/cli copy output.gltf output_fixed.glb
```

### Method 2: Design Automation API

```bash
python3 converters/design-automation/convert_da.py input_dwg/your_file.dwg [-o output_dir]
```

This uploads the DWG, runs AutoCAD in the cloud with the STL exporter plugin, downloads the resulting STL, and converts it to GLB. Output defaults to `output_glb_da/`.

## Project Structure

```
converters/
  aps/
    convert_aps.py           -- Upload + translate DWG via Model Derivative API
    svf_to_glb.js            -- Extract GLB from SVF translation result
  design-automation/
    convert_da.py            -- Full DWG to GLB pipeline via Design Automation API
    setup_da.py              -- One-time plugin registration and activity setup
    plugin/
      Commands.cs            -- AutoCAD C# plugin that exports entities to STL
      StlExporter.csproj     -- .NET project file for the plugin
      PackageContents.xml    -- AutoCAD plugin manifest
  opensource-attempts/        -- Failed open-source approaches (archived)
docs/
  process-explained.md       -- Detailed explanation of the conversion process
report_dwg_to_glb.md        -- Method comparison report
```

## Requirements

- Python 3.12+
- Node.js 18+ (for Method 1 only)
- .NET SDK (for building the Design Automation plugin)
- Autodesk developer account (free tier available)
- Internet connection (cloud-based translation)
