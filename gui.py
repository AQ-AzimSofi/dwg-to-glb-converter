"""
DWG/FBX to GLB Converter GUI.

Simple tkinter app that lets users:
1. Select an input file (DWG, DXF, or FBX)
2. Auto-detects if colors are present
3. If no colors, prompts for a color source file (DWG/DXF)
4. Choose conversion method
5. Convert and see real-time log output
"""

import importlib.util
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = PROJECT_ROOT / "output_glb_gui"


def load_module(name, path):
    """Import a Python module from an absolute file path."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def probe_fbx_colors(fbx_path):
    """Check if an FBX file has meaningful material colors. Returns (has_colors, details)."""
    try:
        import pyassimp
        with pyassimp.load(fbx_path, processing=0) as scene:
            unique = set()
            names = []
            for mat in scene.materials:
                props = dict(mat.properties)
                diff = props.get("diffuse", [0.8, 0.8, 0.8])
                rgb = tuple(round(c, 2) for c in diff[:3])
                name = props.get("name", "?")
                names.append(name)
                if rgb != (0.8, 0.8, 0.8) and rgb != (0.0, 0.0, 0.0):
                    unique.add(rgb)
            n_mats = len(scene.materials)
            n_meshes = len(scene.meshes)
            has_colors = len(unique) >= 2
            detail = f"{n_meshes} meshes, {n_mats} materials, {len(unique)} unique colors"
            return has_colors, detail
    except Exception as e:
        return False, f"Error reading FBX: {e}"


def probe_dwg_colors(path):
    """Check layer colors in a DWG/DXF file. Returns (layer_count, details)."""
    try:
        import ezdxf
        doc = ezdxf.readfile(path)
        layers = [(l.dxf.name, l.color) for l in doc.layers]
        return len(layers), f"{len(layers)} layers with color data"
    except Exception as e:
        return 0, f"Error reading file: {e}"


class QueueWriter:
    """Redirects writes to a queue for thread-safe log output."""

    def __init__(self, q):
        self.queue = q

    def write(self, text):
        if text and text.strip():
            self.queue.put(text)

    def flush(self):
        pass


class ConverterApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("DWG/FBX to GLB Converter")
        self.geometry("750x700")
        self.resizable(True, True)

        self.log_queue = queue.Queue()
        self.conversion_thread = None

        style = ttk.Style()
        style.configure("TButton", padding=5)
        style.configure("Header.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 9))

        self._build_ui()
        self._poll_log()

    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}
        main = ttk.Frame(self, padding=10)
        main.pack(fill="both", expand=True)

        # --- Input file ---
        input_frame = ttk.LabelFrame(main, text="Input File", padding=8)
        input_frame.pack(fill="x", **pad)

        row1 = ttk.Frame(input_frame)
        row1.pack(fill="x")
        self.input_var = tk.StringVar()
        self.input_entry = ttk.Entry(row1, textvariable=self.input_var, state="readonly")
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(row1, text="Browse...", command=self._browse_input).pack(side="right")

        self.input_status = ttk.Label(input_frame, text="No file selected", style="Status.TLabel")
        self.input_status.pack(anchor="w", pady=(4, 0))

        # --- Color source ---
        self.color_frame = ttk.LabelFrame(main, text="Color Source (DWG/DXF)", padding=8)
        self.color_frame.pack(fill="x", **pad)

        row2 = ttk.Frame(self.color_frame)
        row2.pack(fill="x")
        self.color_var = tk.StringVar()
        self.color_entry = ttk.Entry(row2, textvariable=self.color_var, state="readonly")
        self.color_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.color_browse_btn = ttk.Button(row2, text="Browse...", command=self._browse_color)
        self.color_browse_btn.pack(side="right")

        self.color_status = ttk.Label(self.color_frame, text="Not needed yet", style="Status.TLabel")
        self.color_status.pack(anchor="w", pady=(4, 0))

        self._set_color_source_enabled(False)

        # --- Method ---
        method_frame = ttk.LabelFrame(main, text="Conversion Method", padding=8)
        method_frame.pack(fill="x", **pad)

        self.method_var = tk.StringVar(value="")
        self.method_buttons = {}

        methods = [
            ("local_fbx", "Local: FBX to GLB (colors from FBX materials)"),
            ("local_fbx_dwg", "Local: FBX + DWG/DXF to GLB (colors from layers)"),
            ("da_fbx", "Cloud: Design Automation (DWG -> 3ds Max -> GLB)"),
        ]
        for val, label in methods:
            rb = ttk.Radiobutton(method_frame, text=label, variable=self.method_var, value=val)
            rb.pack(anchor="w", pady=1)
            rb.configure(state="disabled")
            self.method_buttons[val] = rb

        # --- Output ---
        out_frame = ttk.LabelFrame(main, text="Output Directory", padding=8)
        out_frame.pack(fill="x", **pad)

        row3 = ttk.Frame(out_frame)
        row3.pack(fill="x")
        self.output_var = tk.StringVar(value=str(DEFAULT_OUTPUT))
        self.output_entry = ttk.Entry(row3, textvariable=self.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(row3, text="Browse...", command=self._browse_output).pack(side="right")

        # --- Convert button ---
        self.convert_btn = ttk.Button(
            main, text="Convert", command=self._start_conversion, style="TButton"
        )
        self.convert_btn.pack(fill="x", padx=10, pady=(8, 4))
        self.convert_btn.configure(state="disabled")

        # --- Progress ---
        self.progress = ttk.Progressbar(main, mode="indeterminate")
        self.progress.pack(fill="x", **pad)

        # --- Log ---
        log_frame = ttk.LabelFrame(main, text="Log", padding=4)
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=12, font=("Consolas", 9), state="disabled", wrap="word"
        )
        self.log_text.pack(fill="both", expand=True)

    def _set_color_source_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        self.color_browse_btn.configure(state=state)
        if not enabled:
            self.color_var.set("")
            self.color_status.configure(text="Not needed -- primary file has color data")

    def _browse_input(self):
        path = filedialog.askopenfilename(
            title="Select Input File",
            filetypes=[
                ("CAD/3D files", "*.dwg *.dxf *.fbx"),
                ("DWG files", "*.dwg"),
                ("DXF files", "*.dxf"),
                ("FBX files", "*.fbx"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.input_var.set(path)
            self._analyze_input(path)

    def _browse_color(self):
        path = filedialog.askopenfilename(
            title="Select Color Source (DWG/DXF)",
            filetypes=[
                ("DWG/DXF files", "*.dwg *.dxf"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.color_var.set(path)
            ext = Path(path).suffix.lower()
            if ext == ".dwg":
                self.color_status.configure(text="DWG selected (will convert to DXF for color reading)")
            else:
                count, detail = probe_dwg_colors(path)
                self.color_status.configure(text=detail)
            self._update_methods()

    def _browse_output(self):
        path = filedialog.askdirectory(title="Select Output Directory")
        if path:
            self.output_var.set(path)

    def _analyze_input(self, path):
        ext = Path(path).suffix.lower()

        for btn in self.method_buttons.values():
            btn.configure(state="disabled")
        self.method_var.set("")
        self.convert_btn.configure(state="disabled")

        if ext == ".fbx":
            self.input_status.configure(text="Checking for color data...")
            self.progress.start(15)
            threading.Thread(
                target=self._probe_fbx_thread, args=(path,), daemon=True
            ).start()

        elif ext in (".dwg", ".dxf"):
            self.input_status.configure(text="Reading layer colors...")
            self.progress.start(15)
            threading.Thread(
                target=self._probe_dwg_thread, args=(path, ext), daemon=True
            ).start()

        else:
            self.input_status.configure(text=f"Unsupported file type: {ext}")

    def _probe_fbx_thread(self, path):
        has_colors, detail = probe_fbx_colors(path)
        self.after(0, self._on_fbx_probed, has_colors, detail)

    def _on_fbx_probed(self, has_colors, detail):
        self.progress.stop()
        if has_colors:
            self.input_status.configure(text=f"FBX with colors: {detail}")
            self._set_color_source_enabled(False)
        else:
            self.input_status.configure(text=f"FBX (no colors): {detail}")
            self._set_color_source_enabled(True)
            self.color_status.configure(text="Select a DWG or DXF file for layer colors")
        self._update_methods()

    def _probe_dwg_thread(self, path, ext):
        if ext == ".dxf":
            count, detail = probe_dwg_colors(path)
        else:
            count, detail = 0, "DWG file (layer colors available)"
        self.after(0, self._on_dwg_probed, ext, detail)

    def _on_dwg_probed(self, ext, detail):
        self.progress.stop()
        if ext == ".dxf":
            self.input_status.configure(text=f"DXF: {detail}")
        else:
            self.input_status.configure(text=detail)
        self._set_color_source_enabled(False)
        self._update_methods()

    def _update_methods(self):
        for btn in self.method_buttons.values():
            btn.configure(state="disabled")
        self.method_var.set("")

        path = self.input_var.get()
        if not path:
            return

        ext = Path(path).suffix.lower()
        available = []

        if ext == ".fbx":
            has_colors, _ = probe_fbx_colors(path)
            if has_colors:
                available.append("local_fbx")
            color_path = self.color_var.get()
            if color_path:
                available.append("local_fbx_dwg")
            if not has_colors and not color_path:
                self.convert_btn.configure(state="disabled")
                return

        elif ext == ".dwg":
            available.append("da_fbx")

        for method in available:
            self.method_buttons[method].configure(state="normal")

        if available:
            self.method_var.set(available[0])
            self.convert_btn.configure(state="normal")

    def _log(self, text):
        self.log_queue.put(text)

    def _poll_log(self):
        while True:
            try:
                msg = self.log_queue.get_nowait()
                if msg == "__DONE__":
                    self._on_conversion_done()
                    continue
                if msg.startswith("__ERROR__"):
                    self._on_conversion_error(msg[9:])
                    continue
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg if msg.endswith("\n") else msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
            except queue.Empty:
                break
        self.after(100, self._poll_log)

    def _start_conversion(self):
        method = self.method_var.get()
        primary = self.input_var.get()
        color_source = self.color_var.get()
        output_dir = self.output_var.get()

        if not primary:
            messagebox.showwarning("Missing input", "Select an input file first.")
            return
        if not method:
            messagebox.showwarning("Missing method", "Select a conversion method.")
            return

        os.makedirs(output_dir, exist_ok=True)

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

        self.convert_btn.configure(state="disabled")
        self.progress.start(15)

        self.conversion_thread = threading.Thread(
            target=self._run_conversion,
            args=(method, primary, color_source, output_dir),
            daemon=True,
        )
        self.conversion_thread.start()

    def _run_conversion(self, method, primary, color_source, output_dir):
        old_stdout = sys.stdout
        sys.stdout = QueueWriter(self.log_queue)
        try:
            if method == "local_fbx":
                self._convert_local_fbx(primary, output_dir)
            elif method == "local_fbx_dwg":
                self._convert_local_fbx_dwg(primary, color_source, output_dir)
            elif method == "da_fbx":
                self._convert_da_fbx(primary, output_dir)
            self.log_queue.put("__DONE__")
        except SystemExit:
            self.log_queue.put("__ERROR__Conversion process exited with an error.")
        except Exception as e:
            self.log_queue.put(f"__ERROR__{e}")
        finally:
            sys.stdout = old_stdout

    def _convert_local_fbx(self, fbx_path, output_dir):
        """FBX (with embedded colors) -> GLB."""
        mod = load_module(
            "convert_da_fbx",
            PROJECT_ROOT / "converters" / "design-automation-fbx" / "convert_da_fbx.py",
        )
        stem = Path(fbx_path).stem
        glb_path = str(Path(output_dir) / f"{stem}.glb")
        print(f"[pipeline] Local FBX -> GLB")
        print(f"[input] {fbx_path}")
        mod.fbx_to_glb(fbx_path, glb_path)

    def _convert_local_fbx_dwg(self, fbx_path, color_path, output_dir):
        """FBX (grey) + DWG/DXF (colors) -> GLB."""
        mod = load_module(
            "convert_fbx_dwg",
            PROJECT_ROOT / "converters" / "fbx-dwg-to-glb" / "convert.py",
        )
        stem = Path(fbx_path).stem
        glb_path = str(Path(output_dir) / f"{stem}.glb")
        print(f"[pipeline] Local FBX + DWG/DXF -> GLB")
        print(f"[input] FBX: {fbx_path}")
        print(f"[input] Color source: {color_path}")
        layer_colors, block_to_layer = mod.build_layer_colors(color_path)
        print(f"[color] {len(layer_colors)} layers, {len(block_to_layer)} block mappings")
        for name, rgba in layer_colors.items():
            print(f"  {name}: RGB({rgba[0]},{rgba[1]},{rgba[2]})")
        mod.fbx_to_glb(fbx_path, glb_path, layer_colors, block_to_layer)

    def _convert_da_fbx(self, dwg_path, output_dir):
        """DWG -> 3ds Max DA (cloud) -> FBX -> GLB."""
        mod = load_module(
            "convert_da_fbx",
            PROJECT_ROOT / "converters" / "design-automation-fbx" / "convert_da_fbx.py",
        )

        if not mod.CLIENT_ID or not mod.CLIENT_SECRET:
            raise RuntimeError("APS credentials not found. Set APS_CLIENT_ID and APS_CLIENT_SECRET in .env")

        import hashlib
        stem = Path(dwg_path).stem
        fbx_key = hashlib.md5(stem.encode()).hexdigest()[:12] + ".fbx"
        log_key = hashlib.md5(stem.encode()).hexdigest()[:12] + ".log.txt"

        print(f"[pipeline] Cloud: DWG -> 3ds Max DA -> FBX -> GLB")
        print(f"[input] {dwg_path}")

        token = mod.get_token()
        mod.ensure_bucket(token)
        _, dwg_key = mod.upload_file(token, dwg_path)

        dwg_url = mod.get_signed_download_url(token, dwg_key)
        fbx_url, fbx_upload_key = mod.get_signed_upload_url(token, fbx_key)
        log_url, log_upload_key = mod.get_signed_upload_url(token, log_key)

        workitem_id = mod.submit_workitem(token, dwg_url, fbx_url, log_upload_url=log_url)
        mod.poll_workitem(token, workitem_id)

        import requests
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        requests.post(
            f"{mod.APS_BASE}/oss/v2/buckets/{mod.BUCKET_KEY}/objects/{fbx_key}/signeds3upload",
            headers=headers, json={"uploadKey": fbx_upload_key},
        )
        requests.post(
            f"{mod.APS_BASE}/oss/v2/buckets/{mod.BUCKET_KEY}/objects/{log_key}/signeds3upload",
            headers=headers, json={"uploadKey": log_upload_key},
        )

        fbx_path = str(Path(output_dir) / f"{stem}.fbx")
        glb_path = str(Path(output_dir) / f"{stem}.glb")
        log_path = str(Path(output_dir) / f"{stem}.log.txt")

        mod.download_fbx(token, fbx_key, fbx_path)

        try:
            mod.download_fbx(token, log_key, log_path)
            print(f"[log] DA log saved to {log_path}")
        except Exception:
            pass

        mod.fbx_to_glb(fbx_path, glb_path)

    def _on_conversion_done(self):
        self.progress.stop()
        self.convert_btn.configure(state="normal")
        self._log("[done] Conversion complete!")
        output_dir = self.output_var.get()
        messagebox.showinfo("Done", f"GLB saved to:\n{output_dir}")

    def _on_conversion_error(self, error_msg):
        self.progress.stop()
        self.convert_btn.configure(state="normal")
        self._log(f"[error] {error_msg}")
        messagebox.showerror("Error", str(error_msg))


if __name__ == "__main__":
    app = ConverterApp()
    app.mainloop()
