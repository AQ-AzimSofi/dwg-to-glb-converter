"""
One-time setup for Design Automation FBX pipeline (3ds Max engine).

Steps:
1. Package MAXScript as AppBundle ZIP
2. Register nickname with APS
3. Upload AppBundle
4. Create Activity
"""

import base64
import io
import os
import sys
import zipfile
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

APS_BASE = "https://developer.api.autodesk.com"
DA_BASE = f"{APS_BASE}/da/us-east/v3"
CLIENT_ID = os.environ.get("APS_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("APS_CLIENT_SECRET", "")
NICKNAME = CLIENT_ID[:12].lower()
ENGINE = "Autodesk.3dsMax+2025"
APPBUNDLE_NAME = "FbxExporter"
ACTIVITY_NAME = "ExportToFbx"

SCRIPT_DIR = Path(__file__).resolve().parent


def get_token():
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    resp = requests.post(
        f"{APS_BASE}/authentication/v2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data="grant_type=client_credentials&scope=data:read data:write data:create bucket:create bucket:read code:all",
    )
    resp.raise_for_status()
    print("[auth] got token")
    return resp.json()["access_token"]


def package_appbundle():
    print("[package] creating AppBundle ZIP...")
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(
            SCRIPT_DIR / "PackageContents.xml",
            f"{APPBUNDLE_NAME}.bundle/PackageContents.xml",
        )
        zf.write(
            SCRIPT_DIR / "export_fbx.ms",
            f"{APPBUNDLE_NAME}.bundle/Contents/export_fbx.ms",
        )

    buf.seek(0)
    print(f"[package] ZIP created ({buf.getbuffer().nbytes / 1024:.1f} KB)")
    return buf


def register_nickname(token):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.patch(
        f"{DA_BASE}/forgeapps/me",
        headers=headers,
        json={"nickname": NICKNAME},
    )
    if resp.status_code == 200:
        print(f"[nickname] registered: {NICKNAME}")
    elif resp.status_code == 409:
        print("[nickname] already registered")
    else:
        print(f"[nickname] response: {resp.status_code} {resp.text}")
        resp.raise_for_status()


def create_appbundle(token, zip_buf):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    resp = requests.delete(
        f"{DA_BASE}/appbundles/{APPBUNDLE_NAME}", headers=headers
    )
    if resp.status_code == 204:
        print(f"[appbundle] deleted existing '{APPBUNDLE_NAME}'")

    resp = requests.post(
        f"{DA_BASE}/appbundles",
        headers=headers,
        json={
            "id": APPBUNDLE_NAME,
            "engine": ENGINE,
            "description": "Exports DWG to FBX via 3ds Max",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"[appbundle] created v{data.get('version', '?')}")

    upload_params = data.get("uploadParameters", {})
    upload_url = upload_params.get("endpointURL", "")
    form_data = upload_params.get("formData", {})

    print("[appbundle] uploading ZIP...")
    zip_buf.seek(0)
    resp = requests.post(
        upload_url,
        data=form_data,
        files={"file": ("appbundle.zip", zip_buf, "application/octet-stream")},
    )
    resp.raise_for_status()
    print("[appbundle] uploaded")

    resp = requests.post(
        f"{DA_BASE}/appbundles/{APPBUNDLE_NAME}/aliases",
        headers=headers,
        json={"id": "prod", "version": data["version"]},
    )
    if resp.status_code == 409:
        resp = requests.patch(
            f"{DA_BASE}/appbundles/{APPBUNDLE_NAME}/aliases/prod",
            headers=headers,
            json={"version": data["version"]},
        )
    resp.raise_for_status()
    print(f"[appbundle] alias 'prod' set to v{data['version']}")


def read_script():
    script_path = SCRIPT_DIR / "export_fbx.ms"
    return script_path.read_text(encoding="utf-8")


def create_activity(token):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    resp = requests.delete(
        f"{DA_BASE}/activities/{ACTIVITY_NAME}", headers=headers
    )
    if resp.status_code == 204:
        print(f"[activity] deleted existing '{ACTIVITY_NAME}'")

    script_content = read_script()

    resp = requests.post(
        f"{DA_BASE}/activities",
        headers=headers,
        json={
            "id": ACTIVITY_NAME,
            "engine": ENGINE,
            "commandLine": [
                '$(engine.path)/3dsmaxbatch.exe '
                '"$(settings[script].path)" '
                '-v 5'
            ],
            "appbundles": [],
            "parameters": {
                "inputFile": {
                    "verb": "get",
                    "localName": "input.dwg",
                    "required": True,
                },
                "outputFile": {
                    "verb": "put",
                    "localName": "result.fbx",
                    "required": True,
                },
                "logFile": {
                    "verb": "put",
                    "localName": "log.txt",
                    "required": False,
                },
            },
            "settings": {
                "script": {
                    "value": script_content,
                },
            },
        },
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"[activity] created '{ACTIVITY_NAME}' v{data.get('version', '?')}")

    resp = requests.post(
        f"{DA_BASE}/activities/{ACTIVITY_NAME}/aliases",
        headers=headers,
        json={"id": "prod", "version": data["version"]},
    )
    if resp.status_code == 409:
        resp = requests.patch(
            f"{DA_BASE}/activities/{ACTIVITY_NAME}/aliases/prod",
            headers=headers,
            json={"version": data["version"]},
        )
    resp.raise_for_status()
    print(f"[activity] alias 'prod' set to v{data['version']}")


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Set APS_CLIENT_ID and APS_CLIENT_SECRET in .env")
        sys.exit(1)

    token = get_token()
    register_nickname(token)
    create_activity(token)

    print("\n[done] setup complete!")
    print(f"  Nickname:  {NICKNAME}")
    print(f"  Activity:  {NICKNAME}.{ACTIVITY_NAME}+prod")
    print(f"\nRun convert_da_fbx.py to convert DWG files.")


if __name__ == "__main__":
    main()
