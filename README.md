# DWG to GLB Converter

Convert AutoCAD DWG files to GLB (binary glTF) using the Autodesk Platform Services (APS) API.

## Why APS?

DWG files containing 3DSOLID entities store geometry in the proprietary ACIS format. No open-source tool can tessellate ACIS data into triangle meshes. The APS Model Derivative API is the only programmatic path that works, because Autodesk's server-side engine includes the ACIS kernel.

See [report_dwg_to_glb.md](report_dwg_to_glb.md) for a detailed comparison of all methods tested.

## Setup

### 1. APS credentials

Create an Autodesk developer account and an APS application with **Data Management API** and **Model Derivative API** enabled.

Copy the example env file and fill in your credentials:

```bash
cp .env.example .env
# Edit .env with your APS_CLIENT_ID and APS_CLIENT_SECRET
```

### 2. Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests python-dotenv
```

### 3. Node.js dependencies

```bash
npm install
```

## Usage

### Step 1: Upload DWG and translate to SVF

```bash
source .venv/bin/activate
python3 converters/aps/convert_aps.py input_dwg/your_file.dwg
```

This uploads the DWG to Autodesk's cloud and translates it to SVF format. It outputs a base64 URN.

### Step 2: Extract GLB from SVF

```bash
node converters/aps/svf_to_glb.js <urn> [output_dir]
```

### Step 3: Package as GLB (if needed)

```bash
npx gltf-pipeline -i output_glb_aps/output.gltf -o output_glb_aps/output.glb
```

For complex models with validation errors:

```bash
npx @gltf-transform/cli copy output.gltf output_fixed.glb
```

## Project Structure

```
converters/
  aps/
    convert_aps.py       -- Upload + translate DWG via APS API
    svf_to_glb.js        -- Extract GLB from SVF translation result
  opensource-attempts/    -- Failed open-source approaches (archived)
    trimesh/             -- ezdxf + trimesh (cannot tessellate ACIS)
    blender/             -- Blender 4.2 (DXF addon removed)
    freecad/             -- FreeCAD + ODA (cannot read ACIS)
docs/                    -- Additional documentation
report_dwg_to_glb.md    -- Method comparison report
```

## Requirements

- Python 3.12+
- Node.js 18+
- Autodesk developer account (free tier available)
- Internet connection (cloud-based translation)
