"""
One-time setup for Design Automation API.

Steps:
1. Build the C# plugin
2. Package as AppBundle ZIP
3. Register nickname with APS
4. Upload AppBundle
5. Create Activity
"""

import base64
import io
import json
import os
import subprocess
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
ENGINE = "Autodesk.AutoCAD+24_3"
APPBUNDLE_NAME = "StlExporter"
ACTIVITY_NAME = "ExportToStl"

PLUGIN_DIR = Path(__file__).resolve().parent / "plugin"


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
    token = resp.json()["access_token"]
    print(f"[auth] got token")
    return token


def build_plugin():
    print("[build] building C# plugin...")
    dotnet = os.path.expanduser("~/.dotnet/dotnet")
    result = subprocess.run(
        [dotnet, "publish", "-c", "Release", "-o", "bin/publish"],
        cwd=str(PLUGIN_DIR),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[build] FAILED:\n{result.stdout}\n{result.stderr}")
        sys.exit(1)
    print("[build] plugin built successfully")


def package_appbundle():
    print("[package] creating AppBundle ZIP...")
    publish_dir = PLUGIN_DIR / "bin" / "publish"
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(
            PLUGIN_DIR / "PackageContents.xml",
            f"{APPBUNDLE_NAME}.bundle/PackageContents.xml",
        )
        for f in publish_dir.iterdir():
            if f.is_file():
                zf.write(f, f"{APPBUNDLE_NAME}.bundle/Contents/{f.name}")

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
        print(f"[nickname] already registered")
    else:
        print(f"[nickname] response: {resp.status_code} {resp.text}")
        resp.raise_for_status()


def create_appbundle(token, zip_buf):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Delete existing AppBundle if any
    resp = requests.delete(
        f"{DA_BASE}/appbundles/{APPBUNDLE_NAME}", headers=headers
    )
    if resp.status_code == 204:
        print(f"[appbundle] deleted existing '{APPBUNDLE_NAME}'")

    # Create new AppBundle
    resp = requests.post(
        f"{DA_BASE}/appbundles",
        headers=headers,
        json={
            "id": APPBUNDLE_NAME,
            "engine": ENGINE,
            "description": "Exports 3DSOLID entities to STL",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"[appbundle] created v{data.get('version', '?')}")

    upload_params = data.get("uploadParameters", {})
    upload_url = upload_params.get("endpointURL", "")
    form_data = upload_params.get("formData", {})

    # Upload ZIP
    print("[appbundle] uploading ZIP...")
    zip_buf.seek(0)
    resp = requests.post(
        upload_url,
        data=form_data,
        files={"file": ("appbundle.zip", zip_buf, "application/octet-stream")},
    )
    resp.raise_for_status()
    print("[appbundle] uploaded")

    # Create alias
    resp = requests.post(
        f"{DA_BASE}/appbundles/{APPBUNDLE_NAME}/aliases",
        headers=headers,
        json={"id": "prod", "version": data["version"]},
    )
    if resp.status_code == 409:
        # Alias exists, update it
        resp = requests.patch(
            f"{DA_BASE}/appbundles/{APPBUNDLE_NAME}/aliases/prod",
            headers=headers,
            json={"version": data["version"]},
        )
    resp.raise_for_status()
    print(f"[appbundle] alias 'prod' set to v{data['version']}")


def create_activity(token):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Delete existing Activity if any
    resp = requests.delete(
        f"{DA_BASE}/activities/{ACTIVITY_NAME}", headers=headers
    )
    if resp.status_code == 204:
        print(f"[activity] deleted existing '{ACTIVITY_NAME}'")

    appbundle_ref = f"{NICKNAME}.{APPBUNDLE_NAME}+prod"

    resp = requests.post(
        f"{DA_BASE}/activities",
        headers=headers,
        json={
            "id": ACTIVITY_NAME,
            "engine": ENGINE,
            "commandLine": [
                '$(engine.path)\\accoreconsole.exe '
                '/i "$(args[inputFile].path)" '
                '/al "$(appbundles[' + APPBUNDLE_NAME + '].path)" '
                '/s "$(settings[script].path)"'
            ],
            "appbundles": [appbundle_ref],
            "parameters": {
                "inputFile": {
                    "verb": "get",
                    "localName": "input.dwg",
                    "required": True,
                },
                "outputFile": {
                    "verb": "put",
                    "localName": "result.stl",
                    "required": True,
                },
            },
            "settings": {
                "script": {
                    "value": "ExportToStl\n"
                }
            },
        },
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"[activity] created '{ACTIVITY_NAME}' v{data.get('version', '?')}")

    # Create alias
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
    build_plugin()
    zip_buf = package_appbundle()
    register_nickname(token)
    create_appbundle(token, zip_buf)
    create_activity(token)

    print("\n[done] setup complete!")
    print(f"  Nickname:  {NICKNAME}")
    print(f"  AppBundle: {NICKNAME}.{APPBUNDLE_NAME}+prod")
    print(f"  Activity:  {NICKNAME}.{ACTIVITY_NAME}+prod")
    print(f"\nRun convert_da.py to convert DWG files.")


if __name__ == "__main__":
    main()
