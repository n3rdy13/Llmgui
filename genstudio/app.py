"""GenStudio: desktop GUI for image and video generation APIs."""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
import requests
from PIL import Image, ImageDraw, ImageGrab

from . import storage
from .providers import (KEY_URLS, PROVIDER_LABELS, PROVIDERS, Cancelled,
                        ProviderError, test_key)

VERSION = "1.0.0"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv"}
MODE_LABELS = {"t2i": "Text → Image", "edit": "Image Edit", "t2v": "Text → Video", "i2v": "Image → Video"}
DEFAULT_CHOICE = "(default)"
CUSTOM_LABEL = "➕ Custom model…"


# --------------------------------------------------------------------------- small helpers

def open_path(p: str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(p)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception as e:
        messagebox.showerror("Open failed", str(e))


def reveal_path(p: str) -> None:
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
    else:
        open_path(str(Path(p).parent))


def slugify(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return (s[:n].rstrip("-")) or "gen"


def ext_from(resp: requests.Response, url: str) -> str:
    ct = resp.headers.get("content-type", "").split(";")[0].strip()
    table = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif",
             "video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm"}
    if ct in table:
        return table[ct]
    suffix = Path(url.split("?")[0]).suffix.lower().lstrip(".")
    return suffix if suffix and len(suffix) <= 4 else "bin"


def is_video(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXT


_video_thumb_cache: dict[int, Image.Image] = {}


def video_placeholder(size: int) -> Image.Image:
    if size not in _video_thumb_cache:
        img = Image.new("RGB", (size, int(size * 0.5625)), (30, 34, 44))
        d = ImageDraw.Draw(img)
        w, h = img.size
        r = h // 5
        d.ellipse([w / 2 - r, h / 2 - r, w / 2 + r, h / 2 + r], fill=(70, 120, 220))
        d.polygon([(w / 2 - r / 3, h / 2 - r / 2), (w / 2 - r / 3, h / 2 + r / 2), (w / 2 + r / 2, h / 2)], fill="white")
        _video_thumb_cache[size] = img
    return _video_thumb_cache[size]


def load_thumb(path: str, size: int) -> ctk.CTkImage | None:
    try:
        if is_video(path):
            img = video_placeholder(size)
        else:
            img = Image.open(path)
            img.thumbnail((size, size))
            img = img.copy()
        return ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
    except Exception:
        return None


def model_label(m: dict) -> str:
    return f"{m['name']}  ·  {PROVIDER_LABELS.get(m['provider'], m['provider'])}"


# --------------------------------------------------------------------------- job model

class Job:
    def __init__(self, panel, model, prompt, images, options, extra):
        self.id = uuid.uuid4().hex[:12]
        self.panel = panel
        self.model = model
        self.prompt = prompt
        self.images = list(images)
        self.options = options
        self.extra = extra
        self.cancel = threading.Event()
        self.started = time.time()
        self.card: JobCard | None = None
        self.done = False


class JobCard(ctk.CTkFrame):
    def __init__(self, master, app: "GenStudio", job: Job):
        super().__init__(master, corner_radius=10)
        self.app, self.job = app, job
        self.thumbs = []
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(8, 2))
        ctk.CTkLabel(top, text=f"{job.model['name']}  ·  {PROVIDER_LABELS.get(job.model['provider'])}",
                     font=ctk.CTkFont(weight="bold")).pack(side="left")
        self.cancel_btn = ctk.CTkButton(top, text="Cancel", width=70, height=24, fg_color="#7a2b2b",
                                        hover_color="#9a3434", command=self.on_cancel)
        self.cancel_btn.pack(side="right")
        self.time_lbl = ctk.CTkLabel(top, text="0s", text_color="gray")
        self.time_lbl.pack(side="right", padx=8)
        ctk.CTkLabel(self, text=job.prompt[:220] + ("…" if len(job.prompt) > 220 else ""), wraplength=560,
                     justify="left", anchor="w", text_color=("gray20", "gray75")).pack(fill="x", padx=10)
        self.status = ctk.CTkLabel(self, text="Queued…", anchor="w")
        self.status.pack(fill="x", padx=10)
        self.bar = ctk.CTkProgressBar(self, mode="indeterminate", height=6)
        self.bar.pack(fill="x", padx=10, pady=(0, 8))
        self.bar.start()
        self.out = ctk.CTkFrame(self, fg_color="transparent", height=0)

    def tick(self):
        if not self.job.done:
            self.time_lbl.configure(text=f"{int(time.time() - self.job.started)}s")

    def on_cancel(self):
        self.job.cancel.set()
        self.status.configure(text="Cancelling…")
        self.cancel_btn.configure(state="disabled")

    def _finish(self):
        self.job.done = True
        self.bar.stop()
        self.bar.pack_forget()
        self.cancel_btn.pack_forget()
        self.time_lbl.configure(text=f"{int(time.time() - self.job.started)}s")

    def set_error(self, msg: str):
        self._finish()
        self.status.configure(text=msg, text_color="#e06c6c", wraplength=560, justify="left")
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkButton(btns, text="Copy error", width=90, height=24,
                      command=lambda: (self.clipboard_clear(), self.clipboard_append(msg))).pack(side="left")
        docs = model_docs_url(self.job.model)
        if docs:
            ctk.CTkButton(btns, text="Model docs", width=90, height=24,
                          command=lambda: webbrowser.open(docs)).pack(side="left", padx=6)

    def set_done(self, files: list[str]):
        self._finish()
        est = estimate_cost(self.job)
        self.status.configure(text=f"Done · {len(files)} file(s)" + (f" · ≈${est:.2f}" if est else ""),
                              text_color="#6cc070")
        self.out.pack(fill="x", padx=6, pady=(0, 6))
        for i, f in enumerate(files):
            self.add_output(f, i)

    def add_output(self, path: str, i: int):
        cell = ctk.CTkFrame(self.out, fg_color="transparent")
        cell.grid(row=i // 3, column=i % 3, padx=4, pady=4, sticky="n")
        img = load_thumb(path, 220)
        if img:
            self.thumbs.append(img)
            lbl = ctk.CTkLabel(cell, image=img, text="", cursor="hand2")
            lbl.pack()
            lbl.bind("<Button-1>", lambda e, p=path: open_path(p))
        row = ctk.CTkFrame(cell, fg_color="transparent")
        row.pack(pady=(4, 0))
        ctk.CTkButton(row, text="Open", width=50, height=22, command=lambda: open_path(path)).pack(side="left", padx=1)
        ctk.CTkButton(row, text="Folder", width=50, height=22, command=lambda: reveal_path(path)).pack(side="left", padx=1)
        if not is_video(path):
            ctk.CTkButton(row, text="→Edit", width=50, height=22,
                          command=lambda: self.app.send_to("edit", path)).pack(side="left", padx=1)
            ctk.CTkButton(row, text="→Video", width=54, height=22,
                          command=lambda: self.app.send_to("i2v", path)).pack(side="left", padx=1)


def estimate_cost(job: "Job") -> float:
    est = job.model.get("est_usd") or 0
    count = job.options.get("num_images") or job.options.get("num_outputs") or job.options.get("n") or 1
    if job.model.get("mode") in ("t2i", "edit") and isinstance(count, int):
        est *= count
    return round(est, 4)


def model_docs_url(m: dict) -> str | None:
    if m.get("docs"):
        return m["docs"]
    if m["provider"] == "fal":
        return f"https://fal.ai/models/{m['id']}/api"
    if m["provider"] == "replicate":
        return f"https://replicate.com/{m['id'].split(':')[0]}/api"
    if m["provider"] == "openai":
        return "https://platform.openai.com/docs/guides/image-generation"
    if m["provider"] == "google":
        return "https://ai.google.dev/gemini-api/docs/models"
    return None


# --------------------------------------------------------------------------- generation panel

class GenPanel(ctk.CTkFrame):
    """One tab: model picker + options + prompt on the left, job results on the right."""

    def __init__(self, master, app: "GenStudio", modes: list[str]):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.modes = modes
        self.mode = modes[0]
        self.images: list[str] = []
        self.image_thumbs = []
        self.option_widgets: dict[str, tuple[ctk.CTkOptionMenu, dict]] = {}
        self.label_to_model: dict[str, dict] = {}

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(self, width=390)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 8))
        self.left = left

        if len(modes) > 1:
            self.mode_switch = ctk.CTkSegmentedButton(left, values=[MODE_LABELS[m] for m in modes],
                                                      command=self.on_mode_switch)
            self.mode_switch.set(MODE_LABELS[self.mode])
            self.mode_switch.pack(fill="x", pady=(4, 10))

        ctk.CTkLabel(left, text="Model", font=ctk.CTkFont(weight="bold"), anchor="w").pack(fill="x")
        self.model_menu = ctk.CTkOptionMenu(left, values=["-"], command=self.on_model_change, dynamic_resizing=False)
        self.model_menu.pack(fill="x", pady=(2, 2))
        self.model_info = ctk.CTkLabel(left, text="", anchor="w", text_color="gray", cursor="hand2")
        self.model_info.pack(fill="x")
        self.model_info.bind("<Button-1>", lambda e: self.open_docs())

        # custom model fields
        self.custom = ctk.CTkFrame(left)
        ctk.CTkLabel(self.custom, text="Provider").grid(row=0, column=0, sticky="w", padx=6, pady=2)
        self.c_provider = ctk.CTkOptionMenu(self.custom, values=list(PROVIDERS.keys()), width=150)
        self.c_provider.grid(row=0, column=1, sticky="ew", padx=6, pady=2)
        ctk.CTkLabel(self.custom, text="Model ID").grid(row=1, column=0, sticky="w", padx=6, pady=2)
        self.c_id = ctk.CTkEntry(self.custom, placeholder_text="e.g. fal-ai/flux/dev")
        self.c_id.grid(row=1, column=1, sticky="ew", padx=6, pady=2)
        ctk.CTkLabel(self.custom, text="Image field").grid(row=2, column=0, sticky="w", padx=6, pady=2)
        self.c_img = ctk.CTkEntry(self.custom, placeholder_text="e.g. image_url (blank = none)")
        self.c_img.grid(row=2, column=1, sticky="ew", padx=6, pady=2)
        self.c_list = ctk.CTkCheckBox(self.custom, text="Image field takes a list")
        self.c_list.grid(row=3, column=1, sticky="w", padx=6, pady=(2, 6))
        self.custom.grid_columnconfigure(1, weight=1)

        self.options_frame = ctk.CTkFrame(left, fg_color="transparent")
        self.options_frame.pack(fill="x", pady=(8, 0))

        # image inputs
        self.img_section = ctk.CTkFrame(left, fg_color="transparent")
        ctk.CTkLabel(self.img_section, text="Input image(s)", font=ctk.CTkFont(weight="bold"), anchor="w").pack(fill="x")
        btns = ctk.CTkFrame(self.img_section, fg_color="transparent")
        btns.pack(fill="x", pady=2)
        ctk.CTkButton(btns, text="Add…", width=70, command=self.add_images).pack(side="left")
        ctk.CTkButton(btns, text="Paste", width=70, command=self.paste_image).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Clear", width=70, fg_color="gray30", command=self.clear_images).pack(side="left")
        self.img_list = ctk.CTkFrame(self.img_section, fg_color="transparent")
        self.img_list.pack(fill="x")

        self.prompt_anchor = ctk.CTkLabel(left, text="Prompt", font=ctk.CTkFont(weight="bold"), anchor="w")
        self.prompt_anchor.pack(fill="x", pady=(10, 0))
        self.prompt = ctk.CTkTextbox(left, height=150, wrap="word")
        self.prompt.pack(fill="x", pady=2)
        self.prompt.bind("<Control-Return>", lambda e: (self.generate(), "break")[1])

        self.adv_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(left, text="Advanced JSON parameters", variable=self.adv_var,
                        command=self.toggle_adv).pack(anchor="w", pady=(6, 2))
        self.adv = ctk.CTkTextbox(left, height=90, font=ctk.CTkFont(family="Consolas", size=12))
        self.adv.insert("1.0", "{}")

        self.gen_btn = ctk.CTkButton(left, text="Generate  (Ctrl+Enter)", height=40,
                                     font=ctk.CTkFont(size=15, weight="bold"), command=self.generate)
        self.gen_btn.pack(fill="x", pady=(10, 4))
        self.repeat_menu = ctk.CTkSegmentedButton(left, values=["×1", "×2", "×4"])
        self.repeat_menu.set("×1")
        self.repeat_menu.pack(fill="x", pady=(0, 6))

        # results
        right = ctk.CTkFrame(self)
        right.grid(row=0, column=1, sticky="nsew")
        head = ctk.CTkFrame(right, fg_color="transparent")
        head.pack(fill="x", padx=10, pady=(8, 0))
        ctk.CTkLabel(head, text="Results", font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        ctk.CTkButton(head, text="Clear finished", width=110, height=26, fg_color="gray30",
                      command=self.clear_finished).pack(side="right")
        self.results = ctk.CTkScrollableFrame(right, fg_color="transparent")
        self.results.pack(fill="both", expand=True, padx=4, pady=4)
        self.empty_lbl = ctk.CTkLabel(self.results, text="Your generations will appear here.", text_color="gray")
        self.empty_lbl.pack(pady=40)

        self.refresh_models()

    # ---------------------------------------------------------------- models / options
    def refresh_models(self):
        self.label_to_model.clear()
        models = [m for m in self.app.models if m.get("mode") == self.mode]
        labels = []
        for m in models:
            lbl = model_label(m)
            if not self.app.cfg.get_key(m["provider"]):
                lbl += "  (no key)"
            self.label_to_model[lbl] = m
            labels.append(lbl)
        labels.append(CUSTOM_LABEL)
        self.model_menu.configure(values=labels)
        last = self.app.cfg.get("last_models", {}).get(self.mode)
        pick = next((l for l, m in self.label_to_model.items() if f"{m['provider']}|{m['id']}" == last), None)
        if pick is None:
            pick = next((l for l in labels if "(no key)" not in l and l != CUSTOM_LABEL), labels[0])
        self.model_menu.set(pick)
        self.on_model_change(pick)
        needs_image = self.mode in ("edit", "i2v")
        if needs_image:
            self.img_section.pack(fill="x", pady=(10, 0), before=self.prompt_anchor)
        else:
            self.img_section.pack_forget()

    def on_mode_switch(self, label):
        self.mode = next(m for m in self.modes if MODE_LABELS[m] == label)
        self.refresh_models()

    def current_model(self) -> dict | None:
        lbl = self.model_menu.get()
        if lbl == CUSTOM_LABEL:
            mid = self.c_id.get().strip()
            if not mid:
                return None
            img = self.c_img.get().strip()
            return {"provider": self.c_provider.get(), "id": mid, "name": mid, "mode": self.mode,
                    "image_param": img or None, "image_list": bool(self.c_list.get()), "options": []}
        return self.label_to_model.get(lbl)

    def on_model_change(self, lbl):
        for w in self.options_frame.winfo_children():
            w.destroy()
        self.option_widgets.clear()
        if lbl == CUSTOM_LABEL:
            self.custom.pack(fill="x", pady=4, after=self.model_info)
            self.model_info.configure(text="Put model-specific inputs in Advanced JSON.")
            if not self.adv_var.get():
                self.adv_var.set(True)
                self.toggle_adv()
            return
        self.custom.pack_forget()
        m = self.label_to_model.get(lbl)
        if not m:
            return
        cost = f"≈${m['est_usd']:.3f}/run" if m.get("est_usd", 0) < 0.01 else (f"≈${m['est_usd']:.2f}/run" if m.get("est_usd") else "")
        self.model_info.configure(text=f"{m['id']}   {cost}   ↗ docs")
        self.app.cfg.data.setdefault("last_models", {})[self.mode] = f"{m['provider']}|{m['id']}"
        self.app.cfg.save()
        for i, opt in enumerate(m.get("options", [])):
            ctk.CTkLabel(self.options_frame, text=opt.get("label", opt["key"]), anchor="w", width=80).grid(
                row=i, column=0, sticky="w", pady=2)
            mapping = {DEFAULT_CHOICE: None}
            for c in opt["choices"]:
                mapping[str(c)] = c
            menu = ctk.CTkOptionMenu(self.options_frame, values=list(mapping.keys()), width=200)
            menu.set(DEFAULT_CHOICE)
            menu.grid(row=i, column=1, sticky="ew", pady=2, padx=(6, 0))
            self.option_widgets[opt["key"]] = (menu, mapping)
        self.options_frame.grid_columnconfigure(1, weight=1)

    def open_docs(self):
        m = self.current_model()
        if m and (u := model_docs_url(m)):
            webbrowser.open(u)

    def toggle_adv(self):
        if self.adv_var.get():
            self.adv.pack(fill="x", pady=2, before=self.gen_btn)
        else:
            self.adv.pack_forget()

    # ---------------------------------------------------------------- images
    def add_images(self):
        paths = filedialog.askopenfilenames(title="Choose image(s)",
                                            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"), ("All", "*.*")])
        for p in paths:
            self.add_image_path(p)

    def paste_image(self):
        try:
            clip = ImageGrab.grabclipboard()
        except Exception as e:
            messagebox.showerror("Paste", f"Couldn't read clipboard: {e}")
            return
        if isinstance(clip, Image.Image):
            d = storage.APP_DIR / "clipboard"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"clip_{datetime.now():%Y%m%d_%H%M%S}.png"
            clip.save(p)
            self.add_image_path(str(p))
        elif isinstance(clip, list):
            for p in clip:
                if Path(p).suffix.lower() in IMAGE_EXT:
                    self.add_image_path(p)
        else:
            messagebox.showinfo("Paste", "No image on the clipboard.")

    def add_image_path(self, p: str):
        if self.mode == "i2v":
            self.images = [p]  # video models take one start frame
        elif p not in self.images:
            self.images.append(p)
        self.render_images()

    def clear_images(self):
        self.images = []
        self.render_images()

    def render_images(self):
        for w in self.img_list.winfo_children():
            w.destroy()
        self.image_thumbs = []
        for i, p in enumerate(self.images):
            cell = ctk.CTkFrame(self.img_list, fg_color="transparent")
            cell.grid(row=i // 4, column=i % 4, padx=2, pady=2)
            img = load_thumb(p, 80)
            if img:
                self.image_thumbs.append(img)
                ctk.CTkLabel(cell, image=img, text="").pack()
            ctk.CTkButton(cell, text="✕", width=24, height=18, fg_color="gray30",
                          command=lambda p=p: (self.images.remove(p), self.render_images())).pack(pady=1)

    # ---------------------------------------------------------------- run
    def collect_options(self) -> dict:
        out = {}
        for key, (menu, mapping) in self.option_widgets.items():
            v = mapping.get(menu.get())
            if v is not None:
                out[key] = v
        return out

    def generate(self):
        m = self.current_model()
        if not m:
            messagebox.showwarning("Model", "Pick a model (or enter a custom model ID).")
            return
        m = {**m, "mode": self.mode}
        if not self.app.cfg.get_key(m["provider"]):
            messagebox.showwarning("API key needed",
                                   f"Add your {PROVIDER_LABELS[m['provider']]} API key in the Settings tab first.")
            self.app.tabs.set("Settings")
            return
        prompt = self.prompt.get("1.0", "end").strip()
        if not prompt and self.mode in ("t2i", "t2v", "edit"):
            messagebox.showwarning("Prompt", "Enter a prompt.")
            return
        if self.mode in ("edit", "i2v") and not self.images:
            messagebox.showwarning("Image", "Add an input image first.")
            return
        extra = {}
        if self.adv_var.get():
            raw = self.adv.get("1.0", "end").strip() or "{}"
            try:
                extra = json.loads(raw)
                assert isinstance(extra, dict)
            except Exception:
                messagebox.showerror("Advanced JSON", "Advanced parameters must be a valid JSON object, e.g. {\"seed\": 42}")
                return
        reps = int(self.repeat_menu.get().lstrip("×"))
        for _ in range(reps):
            self.app.start_job(Job(self, m, prompt, self.images, self.collect_options(), extra))

    def add_card(self, job: Job) -> JobCard:
        self.empty_lbl.pack_forget()
        card = JobCard(self.results, self.app, job)
        first = self._first_card(card)
        if first is not None:
            card.pack(fill="x", padx=4, pady=4, before=first)
        else:
            card.pack(fill="x", padx=4, pady=4)
        return card

    def _first_card(self, exclude):
        for w in self.results.pack_slaves():
            if isinstance(w, JobCard) and w is not exclude:
                return w
        return None

    def clear_finished(self):
        for w in list(self.results.winfo_children()):
            if isinstance(w, JobCard) and w.job.done:
                w.destroy()

    def set_prompt(self, text: str):
        self.prompt.delete("1.0", "end")
        self.prompt.insert("1.0", text)


# --------------------------------------------------------------------------- gallery

class GalleryPanel(ctk.CTkFrame):
    COLS = 4
    TILE = 170
    LIMIT = 300

    def __init__(self, master, app: "GenStudio"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.thumbs: dict[str, ctk.CTkImage] = {}
        self.selected: tuple[dict, str] | None = None

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 6))
        self.filter = ctk.CTkSegmentedButton(bar, values=["All", "Images", "Videos"], command=lambda _: self.refresh())
        self.filter.set("All")
        self.filter.pack(side="left")
        self.search = ctk.CTkEntry(bar, placeholder_text="Search prompts…", width=260)
        self.search.pack(side="left", padx=8)
        self.search.bind("<KeyRelease>", lambda e: self.after_idle(self.refresh))
        ctk.CTkButton(bar, text="Refresh", width=80, command=self.refresh).pack(side="left")
        ctk.CTkButton(bar, text="Open output folder", width=140, fg_color="gray30",
                      command=lambda: open_path(self.app.output_dir())).pack(side="left", padx=8)
        self.spend = ctk.CTkLabel(bar, text="", text_color="gray")
        self.spend.pack(side="right")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)
        self.grid_area = ctk.CTkScrollableFrame(body)
        self.grid_area.grid(row=0, column=0, sticky="nsew")
        self.detail = ctk.CTkFrame(body, width=360)
        self.detail.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        self.detail.grid_propagate(False)
        self.build_detail()

    def build_detail(self):
        d = self.detail
        self.d_img = ctk.CTkLabel(d, text="Select an item", text_color="gray", height=300)
        self.d_img.pack(fill="x", padx=10, pady=10)
        self.d_meta = ctk.CTkLabel(d, text="", justify="left", anchor="w", wraplength=330)
        self.d_meta.pack(fill="x", padx=10)
        self.d_prompt = ctk.CTkTextbox(d, height=120, wrap="word")
        self.d_prompt.pack(fill="x", padx=10, pady=6)
        r1 = ctk.CTkFrame(d, fg_color="transparent")
        r1.pack(fill="x", padx=10)
        ctk.CTkButton(r1, text="Open", width=100, command=lambda: self.sel and open_path(self.sel[1])).pack(side="left")
        ctk.CTkButton(r1, text="Show in folder", width=110,
                      command=lambda: self.sel and reveal_path(self.sel[1])).pack(side="left", padx=6)
        ctk.CTkButton(r1, text="Copy prompt", width=100, command=self.copy_prompt).pack(side="left")
        r2 = ctk.CTkFrame(d, fg_color="transparent")
        r2.pack(fill="x", padx=10, pady=6)
        ctk.CTkButton(r2, text="Reuse prompt", width=100, command=self.reuse).pack(side="left")
        self.b_edit = ctk.CTkButton(r2, text="→ Edit", width=70, command=lambda: self.sel and self.app.send_to("edit", self.sel[1]))
        self.b_edit.pack(side="left", padx=6)
        self.b_vid = ctk.CTkButton(r2, text="→ Video", width=70, command=lambda: self.sel and self.app.send_to("i2v", self.sel[1]))
        self.b_vid.pack(side="left")
        ctk.CTkButton(d, text="Remove from history", fg_color="#7a2b2b", hover_color="#9a3434",
                      command=self.remove).pack(fill="x", padx=10, pady=(4, 10))
        self.d_big = None

    @property
    def sel(self):
        return self.selected

    def refresh(self):
        for w in self.grid_area.winfo_children():
            w.destroy()
        q = self.search.get().strip().lower()
        flt = self.filter.get()
        tiles = []
        for e in reversed(self.app.history.items):
            if e.get("status") != "done":
                continue
            if q and q not in e.get("prompt", "").lower() and q not in e.get("model_name", "").lower():
                continue
            for f in e.get("files", []):
                if not os.path.exists(f):
                    continue
                v = is_video(f)
                if (flt == "Images" and v) or (flt == "Videos" and not v):
                    continue
                tiles.append((e, f))
                if len(tiles) >= self.LIMIT:
                    break
            if len(tiles) >= self.LIMIT:
                break
        for i, (e, f) in enumerate(tiles):
            img = self.thumbs.get(f) or load_thumb(f, self.TILE)
            if img is None:
                continue
            self.thumbs[f] = img
            lbl = ctk.CTkLabel(self.grid_area, image=img, text="", cursor="hand2", width=self.TILE, height=self.TILE)
            lbl.grid(row=i // self.COLS, column=i % self.COLS, padx=4, pady=4)
            lbl.bind("<Button-1>", lambda ev, e=e, f=f: self.select(e, f))
            lbl.bind("<Double-Button-1>", lambda ev, f=f: open_path(f))
        if not tiles:
            ctk.CTkLabel(self.grid_area, text="Nothing here yet.", text_color="gray").grid(row=0, column=0, pady=40, padx=40)
        n = sum(1 for e in self.app.history.items if e.get("status") == "done")
        self.spend.configure(text=f"{n} generations · est. total spend ≈${self.app.history.total_spend():.2f}")

    def select(self, e, f):
        self.selected = (e, f)
        img = load_thumb(f, 330)
        self.d_big = img
        self.d_img.configure(image=img, text="")
        dt = datetime.fromtimestamp(e.get("time", 0)).strftime("%Y-%m-%d %H:%M")
        opts = ", ".join(f"{k}={v}" for k, v in (e.get("options") or {}).items()) or "defaults"
        est = e.get("est_usd")
        self.d_meta.configure(text=f"{e.get('model_name')} · {PROVIDER_LABELS.get(e.get('provider'), '')}\n"
                                   f"{MODE_LABELS.get(e.get('mode'), '')} · {dt}"
                                   + (f" · ≈${est:.2f}" if est else "") + f"\n{opts}\n{Path(f).name}")
        self.d_prompt.delete("1.0", "end")
        self.d_prompt.insert("1.0", e.get("prompt", ""))
        state = "disabled" if is_video(f) else "normal"
        self.b_edit.configure(state=state)
        self.b_vid.configure(state=state)

    def copy_prompt(self):
        if self.sel:
            self.clipboard_clear()
            self.clipboard_append(self.sel[0].get("prompt", ""))

    def reuse(self):
        if not self.sel:
            return
        e = self.sel[0]
        mode = e.get("mode", "t2i")
        panel = self.app.panel_for(mode)
        panel.set_prompt(e.get("prompt", ""))
        self.app.tabs.set(self.app.tab_for(mode))

    def remove(self):
        if not self.sel:
            return
        e = self.sel[0]
        if messagebox.askyesno("Remove", "Remove this generation from history? (Files on disk are kept.)"):
            self.app.history.remove(e["id"])
            self.selected = None
            self.d_img.configure(image=None, text="Select an item")
            self.refresh()


# --------------------------------------------------------------------------- settings

class SettingsPanel(ctk.CTkScrollableFrame):
    def __init__(self, master, app: "GenStudio"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.entries = {}
        ctk.CTkLabel(self, text="API keys", font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", pady=(4, 2))
        note = ("Keys are stored in Windows Credential Manager. You only need the providers you plan to use; "
                "fal.ai alone covers most image and video models.")
        ctk.CTkLabel(self, text=note, text_color="gray", wraplength=800, justify="left").pack(anchor="w", pady=(0, 8))
        for p in PROVIDERS:
            self.key_row(p)

        ctk.CTkLabel(self, text="Output", font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", pady=(18, 4))
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x")
        self.out_entry = ctk.CTkEntry(row, width=520)
        self.out_entry.insert(0, app.output_dir())
        self.out_entry.pack(side="left")
        self.out_entry.bind("<FocusOut>", lambda e: self.save_output())
        self.out_entry.bind("<Return>", lambda e: self.save_output())
        ctk.CTkButton(row, text="Browse…", width=90, command=self.browse).pack(side="left", padx=6)
        ctk.CTkButton(row, text="Open", width=70, command=lambda: open_path(app.output_dir())).pack(side="left")

        ctk.CTkLabel(self, text="Appearance", font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", pady=(18, 4))
        ap = ctk.CTkOptionMenu(self, values=["Dark", "Light", "System"], command=self.set_appearance)
        ap.set(app.cfg.get("appearance"))
        ap.pack(anchor="w")

        ctk.CTkLabel(self, text="Model list", font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", pady=(18, 4))
        ctk.CTkLabel(self, text="Models live in an editable models.json file. Add new model IDs from fal.ai/models or "
                                "replicate.com/explore, then click Reload. You can also use “Custom model…” in any tab.",
                     text_color="gray", wraplength=800, justify="left").pack(anchor="w")
        row2 = ctk.CTkFrame(self, fg_color="transparent")
        row2.pack(fill="x", pady=6)
        ctk.CTkButton(row2, text="Edit models.json", command=lambda: open_path(str(storage.USER_MODELS_PATH))).pack(side="left")
        ctk.CTkButton(row2, text="Reload models", command=app.reload_models).pack(side="left", padx=6)
        ctk.CTkButton(row2, text="Reset to defaults", fg_color="gray30", command=self.reset_models).pack(side="left")
        ctk.CTkButton(row2, text="Browse fal models ↗", fg_color="gray30",
                      command=lambda: webbrowser.open("https://fal.ai/models")).pack(side="left", padx=6)

        ctk.CTkLabel(self, text=f"GenStudio v{VERSION} · data folder: {storage.APP_DIR}", text_color="gray").pack(
            anchor="w", pady=(24, 4))

    def key_row(self, provider):
        f = ctk.CTkFrame(self)
        f.pack(fill="x", pady=3)
        ctk.CTkLabel(f, text=PROVIDER_LABELS[provider], width=90, anchor="w",
                     font=ctk.CTkFont(weight="bold")).pack(side="left", padx=10, pady=8)
        e = ctk.CTkEntry(f, width=380, show="•", placeholder_text="paste API key")
        cur = self.app.cfg.get_key(provider)
        if cur:
            e.insert(0, cur)
        e.pack(side="left")
        self.entries[provider] = e
        show = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(f, text="Show", variable=show, width=60,
                        command=lambda: e.configure(show="" if show.get() else "•")).pack(side="left", padx=6)
        status = ctk.CTkLabel(f, text="✓ saved" if cur else "", text_color="#6cc070", wraplength=260, justify="left")
        ctk.CTkButton(f, text="Save", width=60, command=lambda: self.save_key(provider, status)).pack(side="left")
        ctk.CTkButton(f, text="Test", width=60, fg_color="gray30",
                      command=lambda: self.test(provider, status)).pack(side="left", padx=4)
        ctk.CTkButton(f, text="Get key ↗", width=80, fg_color="gray30",
                      command=lambda: webbrowser.open(KEY_URLS[provider])).pack(side="left")
        status.pack(side="left", padx=8)

    def save_key(self, provider, status):
        msg = self.app.cfg.set_key(provider, self.entries[provider].get())
        status.configure(text=msg if self.entries[provider].get().strip() else "Key removed.", text_color="#6cc070")
        self.app.refresh_all_models()

    def test(self, provider, status):
        key = self.entries[provider].get().strip()
        if not key:
            status.configure(text="Enter a key first.", text_color="#e0a050")
            return
        status.configure(text="Testing…", text_color="gray")

        def work():
            ok, msg = test_key(provider, key)
            self.app.events.put(("ui", lambda: status.configure(text=msg, text_color="#6cc070" if ok else "#e06c6c")))
        threading.Thread(target=work, daemon=True).start()

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.app.output_dir())
        if d:
            self.out_entry.delete(0, "end")
            self.out_entry.insert(0, d)
            self.app.cfg.set("output_dir", d)

    def save_output(self):
        d = self.out_entry.get().strip()
        if d and d != self.app.output_dir():
            self.app.cfg.set("output_dir", d)

    def set_appearance(self, v):
        ctk.set_appearance_mode(v)
        self.app.cfg.set("appearance", v)

    def reset_models(self):
        if messagebox.askyesno("Reset", "Replace your models.json with the built-in default list?"):
            storage.reset_models()
            self.app.reload_models()


# --------------------------------------------------------------------------- main window

class GenStudio(ctk.CTk):
    def __init__(self):
        self.cfg = storage.Config()
        ctk.set_appearance_mode(self.cfg.get("appearance"))
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.title(f"GenStudio · AI image & video generator")
        self.geometry("1320x860")
        self.minsize(1000, 680)
        try:
            ico = storage.resource_path("icon.ico")
            if ico.exists() and sys.platform == "win32":
                self.iconbitmap(str(ico))
        except Exception:
            pass

        self.history = storage.History()
        self.models = storage.load_models()
        self.events: queue.Queue = queue.Queue()
        self.jobs: dict[str, Job] = {}

        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        for name in ("Image", "Edit", "Video", "Gallery", "Settings"):
            self.tabs.add(name)
        self.image_panel = GenPanel(self.tabs.tab("Image"), self, ["t2i"])
        self.edit_panel = GenPanel(self.tabs.tab("Edit"), self, ["edit"])
        self.video_panel = GenPanel(self.tabs.tab("Video"), self, ["t2v", "i2v"])
        self.gallery = GalleryPanel(self.tabs.tab("Gallery"), self)
        self.settings = SettingsPanel(self.tabs.tab("Settings"), self)
        for p in (self.image_panel, self.edit_panel, self.video_panel, self.gallery, self.settings):
            p.pack(fill="both", expand=True)
        self.tabs.configure(command=self.on_tab)

        if not any(self.cfg.get_key(p) for p in PROVIDERS):
            self.tabs.set("Settings")
        self.after(100, self.pump)
        self.after(1000, self.tick)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------------------------------------------------------- routing
    def panel_for(self, mode: str) -> GenPanel:
        return {"t2i": self.image_panel, "edit": self.edit_panel}.get(mode, self.video_panel)

    def tab_for(self, mode: str) -> str:
        return {"t2i": "Image", "edit": "Edit"}.get(mode, "Video")

    def send_to(self, mode: str, path: str):
        panel = self.panel_for(mode)
        if mode == "i2v" and panel.mode != "i2v":
            panel.mode_switch.set(MODE_LABELS["i2v"])
            panel.on_mode_switch(MODE_LABELS["i2v"])
        panel.add_image_path(path)
        self.tabs.set(self.tab_for(mode))

    def on_tab(self):
        if self.tabs.get() == "Gallery":
            self.gallery.refresh()

    def output_dir(self) -> str:
        return self.cfg.get("output_dir") or str(storage.DEFAULT_OUTPUT)

    def reload_models(self):
        self.models = storage.load_models()
        self.refresh_all_models()
        messagebox.showinfo("Models", f"Loaded {len(self.models)} models.")

    def refresh_all_models(self):
        for p in (self.image_panel, self.edit_panel, self.video_panel):
            p.refresh_models()

    # ---------------------------------------------------------------- jobs
    def start_job(self, job: Job):
        self.jobs[job.id] = job
        job.card = job.panel.add_card(job)
        threading.Thread(target=self.worker, args=(job,), daemon=True).start()

    def worker(self, job: Job):
        def progress(text):
            self.events.put(("status", job.id, text))
        try:
            key = self.cfg.get_key(job.model["provider"])
            provider = PROVIDERS[job.model["provider"]](key)
            outputs = provider.run(job.model, job.prompt, job.images, job.options, job.extra, progress, job.cancel)
            progress("Saving…")
            files = self.save_outputs(job, outputs)
            self.events.put(("done", job.id, files))
        except Cancelled:
            self.events.put(("error", job.id, "Cancelled."))
        except ProviderError as e:
            self.events.put(("error", job.id, str(e)))
        except requests.RequestException as e:
            self.events.put(("error", job.id, f"Network error: {e}"))
        except Exception as e:  # keep the app alive no matter what
            self.events.put(("error", job.id, f"{type(e).__name__}: {e}"))

    def save_outputs(self, job: Job, outputs: list[dict]) -> list[str]:
        folder = Path(self.output_dir()) / datetime.now().strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H%M%S")
        base = f"{stamp}_{slugify(job.prompt or job.model['name'])}"
        files = []
        for i, o in enumerate(outputs):
            if "data" in o:
                data, ext = o["data"], o.get("ext", "png")
            else:
                r = requests.get(o["url"], timeout=300)
                r.raise_for_status()
                data, ext = r.content, ext_from(r, o["url"])
            path = folder / f"{base}_{i + 1}.{ext}"
            n = 2
            while path.exists():
                path = folder / f"{base}_{i + 1}_{n}.{ext}"
                n += 1
            path.write_bytes(data)
            files.append(str(path))
        return files

    def pump(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "ui":
                    ev[1]()
                    continue
                job = self.jobs.get(ev[1])
                if not job or not job.card:
                    continue
                if kind == "status":
                    if not job.cancel.is_set():
                        job.card.status.configure(text=ev[2])
                elif kind == "done":
                    job.card.set_done(ev[2])
                    self.record(job, "done", files=ev[2])
                elif kind == "error":
                    job.card.set_error(ev[2])
                    if ev[2] != "Cancelled.":
                        self.record(job, "error", error=ev[2])
        except queue.Empty:
            pass
        self.after(100, self.pump)

    def record(self, job: Job, status: str, files=None, error=None):
        est = estimate_cost(job)
        self.history.add({
            "id": job.id, "time": job.started, "status": status, "mode": job.model.get("mode"),
            "provider": job.model["provider"], "model_id": job.model["id"], "model_name": job.model["name"],
            "prompt": job.prompt, "options": {**job.options, **job.extra}, "inputs": job.images,
            "files": files or [], "error": error, "est_usd": est if status == "done" else 0,
            "seconds": round(time.time() - job.started, 1),
        })

    def tick(self):
        for job in self.jobs.values():
            if not job.done and job.card:
                job.card.tick()
        self.after(1000, self.tick)

    def on_close(self):
        running = [j for j in self.jobs.values() if not j.done]
        if running and not messagebox.askyesno(
                "Jobs running", f"{len(running)} generation(s) still running. Quit anyway? "
                                "(You'll still be billed for them.)"):
            return
        self.destroy()


def main():
    if sys.platform == "win32":
        try:  # crisp text on high-DPI screens + own taskbar icon
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("GenStudio.App")
        except Exception:
            pass
    app = GenStudio()
    app.mainloop()


if __name__ == "__main__":
    main()
