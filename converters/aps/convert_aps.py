"""
APS (Autodesk Platform Services) DWG to GLB converter.

Pipeline:
1. Authenticate with APS (2-legged OAuth)
2. Create a transient bucket
3. Upload DWG file
4. Trigger Model Derivative translation to OBJ
5. Poll until complete
6. Download OBJ
7. Convert OBJ to GLB with trimesh
"""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

APS_BASE = "https://developer.api.autodesk.com"
CLIENT_ID = os.environ.get("APS_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("APS_CLIENT_SECRET", "")
BUCKET_KEY = f"matterport-dwg-{CLIENT_ID[:8].lower()}"


def get_token():
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    resp = requests.post(
        f"{APS_BASE}/authentication/v2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data="grant_type=client_credentials&scope=data:read data:write data:create bucket:create bucket:read",
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]
    print(f"[auth] got token (expires in {resp.json()['expires_in']}s)")
    return token


def ensure_bucket(token):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{APS_BASE}/oss/v2/buckets",
        headers=headers,
        json={"bucketKey": BUCKET_KEY, "policyKey": "transient"},
    )
    if resp.status_code == 409:
        print(f"[bucket] '{BUCKET_KEY}' already exists")
    elif resp.status_code == 200:
        print(f"[bucket] created '{BUCKET_KEY}'")
    else:
        resp.raise_for_status()


def upload_file(token, filepath):
    filename = Path(filepath).name
    filesize = os.path.getsize(filepath)
    # Use ASCII-safe object key to avoid URL encoding issues
    import hashlib
    safe_name = hashlib.md5(filename.encode()).hexdigest()[:12] + ".dwg"
    print(f"[upload] uploading {filename} as {safe_name} ({filesize / 1024:.0f} KB)")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
    }
    with open(filepath, "rb") as f:
        resp = requests.put(
            f"{APS_BASE}/oss/v2/buckets/{BUCKET_KEY}/objects/{safe_name}",
            headers=headers,
            data=f,
        )
    if not resp.ok:
        print(f"[upload] PUT failed ({resp.status_code}): {resp.text}")
        print("[upload] trying signed S3 upload...")
        return upload_file_s3(token, filepath, safe_name)
    object_id = resp.json()["objectId"]
    print(f"[upload] done. objectId: {object_id}")
    return object_id


def upload_file_s3(token, filepath, object_key):
    """Upload via the newer direct-to-S3 approach."""
    headers = {"Authorization": f"Bearer {token}"}

    # Step 1: get signed upload URL
    resp = requests.get(
        f"{APS_BASE}/oss/v2/buckets/{BUCKET_KEY}/objects/{object_key}/signeds3upload",
        headers=headers,
        params={"firstPart": 1, "parts": 1},
    )
    resp.raise_for_status()
    data = resp.json()
    upload_key = data["uploadKey"]
    upload_url = data["urls"][0]

    # Step 2: upload to S3
    with open(filepath, "rb") as f:
        s3_resp = requests.put(upload_url, data=f)
    s3_resp.raise_for_status()

    # Step 3: finalize
    resp = requests.post(
        f"{APS_BASE}/oss/v2/buckets/{BUCKET_KEY}/objects/{object_key}/signeds3upload",
        headers={**headers, "Content-Type": "application/json"},
        json={"uploadKey": upload_key},
    )
    resp.raise_for_status()
    object_id = resp.json()["objectId"]
    print(f"[upload] done via S3. objectId: {object_id}")
    return object_id


def safe_urn(object_id):
    return base64.b64encode(object_id.encode()).decode().rstrip("=").replace("+", "-").replace("/", "_")


def translate_to_obj(token, object_id):
    urn = safe_urn(object_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "x-ads-force": "true",
    }
    payload = {
        "input": {"urn": urn},
        "output": {
            "formats": [
                {"type": "svf", "views": ["3d"]},
            ]
        },
    }
    resp = requests.post(
        f"{APS_BASE}/modelderivative/v2/designdata/job",
        headers=headers,
        json=payload,
    )
    resp.raise_for_status()
    print(f"[translate] job submitted (urn: {urn[:40]}...)")
    return urn


def poll_translation(token, urn, timeout=600):
    headers = {"Authorization": f"Bearer {token}"}
    start = time.time()
    while time.time() - start < timeout:
        resp = requests.get(
            f"{APS_BASE}/modelderivative/v2/designdata/{urn}/manifest",
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "unknown")
        progress = data.get("progress", "unknown")
        print(f"[poll] status={status} progress={progress}")

        if status == "success":
            return data
        if status == "failed":
            print("[poll] translation FAILED")
            print(json.dumps(data, indent=2))
            sys.exit(1)

        time.sleep(10)

    print(f"[poll] timed out after {timeout}s")
    sys.exit(1)


def find_obj_derivative(manifest):
    """Walk the manifest to find OBJ derivative URNs."""
    results = []
    for deriv in manifest.get("derivatives", []):
        output_type = deriv.get("outputType", "")
        for child in deriv.get("children", []):
            if child.get("type") == "resource" and child.get("role") == "obj":
                results.append(child["urn"])
            for grandchild in child.get("children", []):
                if grandchild.get("type") == "resource" and grandchild.get("role") == "obj":
                    results.append(grandchild["urn"])
    return results


def find_all_downloadable(manifest):
    """Walk manifest and list all derivatives for debugging."""
    results = []
    for deriv in manifest.get("derivatives", []):
        for child in deriv.get("children", []):
            results.append({
                "type": child.get("type"),
                "role": child.get("role"),
                "urn": child.get("urn", ""),
                "mime": child.get("mime", ""),
                "name": child.get("name", ""),
            })
            for grandchild in child.get("children", []):
                results.append({
                    "type": grandchild.get("type"),
                    "role": grandchild.get("role"),
                    "urn": grandchild.get("urn", ""),
                    "mime": grandchild.get("mime", ""),
                    "name": grandchild.get("name", ""),
                })
    return results


def download_derivative(token, urn, derivative_urn, output_path):
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(
        f"{APS_BASE}/modelderivative/v2/designdata/{urn}/manifest/{derivative_urn}/signedcookies",
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()
    url = data.get("url", "")
    policy = data.get("CloudFront-Policy", "") or resp.cookies.get("CloudFront-Policy", "")
    key_pair = data.get("CloudFront-Key-Pair-Id", "") or resp.cookies.get("CloudFront-Key-Pair-Id", "")
    signature = data.get("CloudFront-Signature", "") or resp.cookies.get("CloudFront-Signature", "")

    cookies = {}
    for cookie in resp.cookies:
        cookies[cookie.name] = cookie.value

    if policy and key_pair and signature:
        cookies["CloudFront-Policy"] = policy
        cookies["CloudFront-Key-Pair-Id"] = key_pair
        cookies["CloudFront-Signature"] = signature

    print(f"[download] fetching {derivative_urn[-40:]}...")
    dl_resp = requests.get(url, cookies=cookies)
    dl_resp.raise_for_status()

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(dl_resp.content)
    print(f"[download] saved {output_path} ({len(dl_resp.content)} bytes)")


def obj_to_glb(obj_path, glb_path):
    import trimesh
    scene = trimesh.load(obj_path)
    scene.export(glb_path, file_type="glb")
    print(f"[convert] saved {glb_path}")


def main():
    parser = argparse.ArgumentParser(description="Convert DWG to GLB via APS")
    parser.add_argument("input", help="Path to DWG file")
    parser.add_argument("-o", "--output", help="Output GLB path")
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    if not input_path.exists():
        print(f"File not found: {input_path}")
        sys.exit(1)

    output_dir = Path(args.output) if args.output else Path("output_glb_aps")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    token = get_token()
    ensure_bucket(token)
    object_id = upload_file(token, str(input_path))
    urn = translate_to_obj(token, object_id)
    manifest = poll_translation(token, urn)

    all_derivs = find_all_downloadable(manifest)
    print(f"\n[manifest] all derivatives ({len(all_derivs)}):")
    for d in all_derivs:
        print(f"  type={d['type']} role={d['role']} mime={d['mime']} name={d['name']}")

    print(f"\n[manifest] full JSON:")
    print(json.dumps(manifest, indent=2))

    obj_urns = find_obj_derivative(manifest)
    if not obj_urns:
        print("[warn] no OBJ derivatives found. Check manifest above.")
        print("[info] Trying to download any geometry resources...")
        for d in all_derivs:
            if d["urn"] and d["role"] in ("obj", "graphics", "mesh"):
                obj_urns.append(d["urn"])

    if not obj_urns:
        print("[error] no downloadable geometry found")
        sys.exit(1)

    stem = input_path.stem
    for i, obj_urn in enumerate(obj_urns):
        suffix = f"_{i}" if len(obj_urns) > 1 else ""
        ext = ".obj"
        if ".stl" in obj_urn.lower():
            ext = ".stl"
        obj_path = output_dir / f"{stem}{suffix}{ext}"
        download_derivative(token, urn, obj_urn, str(obj_path))

        glb_path = output_dir / f"{stem}{suffix}.glb"
        try:
            obj_to_glb(str(obj_path), str(glb_path))
        except Exception as e:
            print(f"[error] OBJ to GLB conversion failed: {e}")


if __name__ == "__main__":
    main()
