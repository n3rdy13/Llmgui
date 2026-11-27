"""Prompt builder: pick options per category, get a prompt formatted for the selected model."""
from __future__ import annotations

import json
import random
import threading
from tkinter import messagebox

import customtkinter as ctk

from . import llm, storage

OPTIONS_PATH = storage.APP_DIR / "prompt_options.json"
PRESETS_PATH = storage.APP_DIR / "prompt_presets.json"

IMG, EDIT, VID = ("t2i",), ("edit",), ("t2v", "i2v")
ALL = IMG + EDIT + VID

# key, label, modes, multi-select, sentence template
CATEGORIES = [
    ("style", "Style & medium", ALL, False, "Style: {}."),
    ("lighting", "Lighting", ALL, False, "Lighting: {}."),
    ("mood", "Mood", ALL, False, "The mood is {}."),
    ("shot", "Shot & angle", IMG + EDIT + VID, False, "Shot: {}."),
    ("lens", "Lens & focus", IMG + EDIT + VID, False, "Lens: {}."),
    ("composition", "Composition", IMG + EDIT, False, "Composition: {}."),
    ("color", "Color", ALL, False, "Color palette: {}."),
    ("camera_move", "Camera movement", VID, False, "Camera movement: {}."),
    ("motion", "Motion", VID, True, "Motion: {}."),
    ("audio", "Audio", VID, True, "Audio: {}."),
    ("quality", "Quality & detail", ALL, True, "Quality: {}."),
    ("preserve", "Keep unchanged", EDIT, True, "Keep unchanged: {}."),
    ("negatives", "Avoid", ALL, True, "Avoid: {}."),
]

DEFAULT_OPTIONS = {
    "style": ["photorealistic photograph", "cinematic film still", "studio product photo", "editorial fashion photo",
              "documentary photo", "vintage 1970s photo", "polaroid snapshot", "oil painting", "watercolor painting",
              "gouache illustration", "digital illustration", "concept art", "anime", "manga ink drawing",
              "comic book art", "stylized 3D animated render", "low-poly 3D", "isometric illustration", "pixel art",
              "flat vector illustration", "line art", "pencil sketch", "charcoal drawing", "art nouveau poster",
              "paper cut-out collage", "claymation", "vaporwave", "cyberpunk"],
    "lighting": ["golden hour sunlight", "blue hour twilight", "soft overcast daylight", "harsh midday sun",
                 "dramatic chiaroscuro", "rim lighting", "backlit silhouette", "neon glow", "candlelight", "firelight",
                 "studio softbox lighting", "three-point studio lighting", "volumetric god rays", "moonlight",
                 "foggy diffused light", "practical lights at night", "bioluminescent glow", "window light"],
    "mood": ["serene", "moody", "melancholic", "joyful", "dreamy", "mysterious", "tense", "epic", "cozy", "eerie",
             "nostalgic", "whimsical", "gritty", "romantic", "majestic", "playful", "dark and brooding"],
    "shot": ["extreme close-up", "close-up portrait", "medium shot", "full body shot", "wide establishing shot",
             "aerial drone view", "macro shot", "over-the-shoulder shot", "low-angle hero shot", "high-angle shot",
             "bird's-eye view", "worm's-eye view", "dutch angle", "eye-level shot"],
    "lens": ["24mm wide-angle lens", "35mm lens", "50mm lens", "85mm portrait lens", "135mm telephoto lens",
             "fisheye lens", "tilt-shift lens", "anamorphic lens with lens flares", "shallow depth of field, f/1.4",
             "deep focus, f/11", "long exposure", "motion blur"],
    "composition": ["rule of thirds", "centered symmetrical composition", "leading lines", "generous negative space",
                    "framed through foreground elements", "golden ratio", "layered depth", "flat lay", "minimalist"],
    "color": ["warm earth tones", "teal and orange", "pastel palette", "black and white", "muted desaturated tones",
              "vibrant saturated colors", "neon magenta and cyan", "sepia", "duotone", "analogous greens",
              "high contrast", "monochromatic blue"],
    "camera_move": ["static locked-off camera", "slow push-in", "slow pull-out", "pan left", "pan right", "tilt up",
                    "tilt down", "orbit around the subject", "tracking shot following the subject", "handheld camera",
                    "drone fly-over", "crane shot rising up", "dolly zoom", "whip pan", "rack focus",
                    "steadicam walk-through", "time-lapse"],
    "motion": ["slow motion", "gentle ambient motion", "fast energetic action", "hair and clothes blowing in the wind",
               "water rippling", "smoke drifting", "clouds moving across the sky", "subject walks toward the camera",
               "subject turns to face the camera", "crowd bustling", "particles floating in the air", "seamless loop"],
    "audio": ["ambient nature sounds", "soft cinematic score", "upbeat music", "rain and distant thunder",
              "city traffic noise", "quiet room tone", "natural spoken dialogue", "crackling fire", "ocean waves",
              "no music"],
    "quality": ["highly detailed", "sharp focus", "8K resolution", "film grain", "natural skin texture",
                "intricate details", "clean and minimal", "professional color grading", "soft focus",
                "award-winning photography"],
    "preserve": ["face and identity", "pose", "background", "clothing", "composition", "text and logos",
                 "lighting", "color palette"],
    "negatives": ["blurry", "low quality", "watermark", "text", "logo", "signature", "extra fingers",
                  "distorted hands", "deformed anatomy", "oversaturated", "cropped", "jpeg artifacts",
                  "duplicate subjects", "bad proportions"],
}

TAG_MODEL_HINTS = ("sdxl", "stable-diffusion", "sd3", "sd-", "lightning", "pony", "juggernaut")


def wants_tags(model: dict | None) -> bool:
    """Tag-style prompts mostly help SDXL-family models. Newer models read plain sentences better."""
    mid = (model or {}).get("id", "").lower()
    return any(h in mid for h in TAG_MODEL_HINTS)


def user_options() -> dict:
    data = storage._read_json(OPTIONS_PATH, {})
    return data if isinstance(data, dict) else {}


def options_for(key: str) -> list[str]:
    """Built-in list followed by anything the user added, minus anything they hid."""
    u = user_options()
    hidden = set(u.get("hide", {}).get(key, []))
    base = [o for o in DEFAULT_OPTIONS.get(key, []) if o not in hidden]
    extra = [o for o in u.get("add", {}).get(key, []) if o not in base]
    return base + extra


def add_option(key: str, text: str) -> bool:
    text = text.strip()
    if not text or text in options_for(key):
        return False
    u = user_options()
    u.setdefault("add", {}).setdefault(key, []).append(text)
    storage._write_json(OPTIONS_PATH, u)
    return True


def ensure_options_file() -> None:
    if not OPTIONS_PATH.exists():
        template = {
            "_help": "Add your own options per category under 'add'. Hide built-ins under 'hide'. "
                     "Keys: " + ", ".join(c[0] for c in CATEGORIES),
            "add": {"style": ["my custom style"]},
            "hide": {},
        }
        storage._write_json(OPTIONS_PATH, template)


def compose(subject: str, selections: dict[str, list[str]], tags: bool, mode: str) -> str:
    subject = subject.strip()
    cats = [c for c in CATEGORIES if mode in c[2]]
    if tags:
        parts = [subject.rstrip(".,; ")] if subject else []
        for key, _label, _m, _multi, _tpl in cats:
            vals = selections.get(key) or []
            if not vals:
                continue
            if key == "negatives":
                parts.append("avoid: " + ", ".join(vals))
            elif key == "preserve":
                parts.append("keep " + ", ".join(vals) + " unchanged")
            else:
                parts.extend(vals)
        return ", ".join(p for p in parts if p)
    parts = []
    if subject:
        parts.append(subject if subject[-1] in ".!?" else subject + ".")
    for key, _label, _m, _multi, tpl in cats:
        vals = selections.get(key) or []
        if vals:
            parts.append(tpl.format(", ".join(vals)))
    return " ".join(parts)


# --------------------------------------------------------------------------- UI

class PromptBuilder(ctk.CTkFrame):
    def __init__(self, master, panel):
        super().__init__(master, corner_radius=8)
        self.panel = panel
        self.mode = panel.mode
        self.model: dict | None = None
        self.vars: dict[str, ctk.StringVar] = {}
        self.menus: dict[str, ctk.CTkOptionMenu] = {}
        self.locks: dict[str, ctk.BooleanVar] = {}
        self.open = False

        self.header = ctk.CTkButton(self, text="▸  Prompt Builder", anchor="w", fg_color="transparent",
                                    hover_color=("gray80", "gray25"), text_color=("gray10", "gray90"),
                                    font=ctk.CTkFont(weight="bold"), command=self.toggle)
        self.header.pack(fill="x")
        self.body = ctk.CTkFrame(self, fg_color="transparent")

        top = ctk.CTkFrame(self.body, fg_color="transparent")
        top.pack(fill="x", padx=6)
        self.fmt = ctk.CTkSegmentedButton(top, values=["Auto", "Sentences", "Tags"], command=lambda _: self.update_preview())
        self.fmt.set("Auto")
        self.fmt.pack(fill="x")
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=(6, 0))
        ctk.CTkButton(row, text="🎲 Random", width=88, command=self.randomize).pack(side="left")
        ctk.CTkButton(row, text="Clear", width=56, fg_color="gray30", command=self.clear).pack(side="left", padx=4)
        ctk.CTkButton(row, text="Insert into prompt", width=120, command=self.insert).pack(side="left")
        row_llm = ctk.CTkFrame(self.body, fg_color="transparent")
        row_llm.pack(fill="x", padx=6, pady=(6, 0))
        self.llm_btn = ctk.CTkButton(row_llm, text="✨ Enhance with LLM", width=150, fg_color="#6b3fa0",
                                     hover_color="#8350bd", command=self.enhance)
        self.llm_btn.pack(side="left")
        self.undo_btn = ctk.CTkButton(row_llm, text="↩ Undo", width=64, fg_color="gray30", state="disabled",
                                      command=self.undo)
        self.undo_btn.pack(side="left", padx=4)
        self.llm_status = ctk.CTkLabel(row_llm, text="", text_color="gray", anchor="w")
        self.llm_status.pack(side="left", padx=4)
        self.prev_state = None

        pr = ctk.CTkFrame(self.body, fg_color="transparent")
        pr.pack(fill="x", padx=6, pady=(6, 0))
        self.preset_menu = ctk.CTkOptionMenu(pr, values=["(presets)"], width=150, command=self.load_preset)
        self.preset_menu.pack(side="left")
        ctk.CTkButton(pr, text="Save…", width=56, command=self.save_preset).pack(side="left", padx=4)
        ctk.CTkButton(pr, text="Delete", width=56, fg_color="gray30", command=self.delete_preset).pack(side="left")

        self.rows = ctk.CTkFrame(self.body, fg_color="transparent")
        self.rows.pack(fill="x", padx=6, pady=(6, 0))

        lists = ctk.CTkFrame(self.body, fg_color="transparent")
        lists.pack(fill="x", padx=6, pady=(4, 0))
        ctk.CTkButton(lists, text="Edit option lists", width=110, fg_color="gray30", command=self.edit_lists).pack(side="left")
        ctk.CTkButton(lists, text="Reload", width=60, fg_color="gray30", command=self.build_rows).pack(side="left", padx=4)

        ctk.CTkLabel(self.body, text="Final prompt preview", anchor="w", text_color="gray").pack(fill="x", padx=8, pady=(6, 0))
        self.preview = ctk.CTkTextbox(self.body, height=90, wrap="word", state="disabled",
                                      text_color=("gray20", "gray80"))
        self.preview.pack(fill="x", padx=6, pady=(0, 8))

        self.build_rows()
        self.refresh_presets()

    # ------------------------------------------------------------ layout
    def toggle(self):
        self.open = not self.open
        self.header.configure(text=("▾" if self.open else "▸") + "  Prompt Builder")
        if self.open:
            self.body.pack(fill="x")
            self.update_preview()
        else:
            self.body.pack_forget()

    def set_mode(self, mode: str):
        if mode != self.mode:
            self.mode = mode
            self.build_rows()

    def set_model(self, model: dict | None):
        self.model = model
        self.update_preview()

    def build_rows(self):
        saved = self.selections()
        for w in self.rows.winfo_children():
            w.destroy()
        self.vars, self.menus, self.locks = {}, {}, {}
        for key, label, modes, multi, _tpl in CATEGORIES:
            if self.mode not in modes:
                continue
            head = ctk.CTkFrame(self.rows, fg_color="transparent")
            head.pack(fill="x", pady=(4, 0))
            ctk.CTkLabel(head, text=label + (" (multi)" if multi else ""), anchor="w",
                         font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
            lock = ctk.BooleanVar(value=False)
            ctk.CTkCheckBox(head, text="keep on 🎲", variable=lock, width=20, checkbox_width=14, checkbox_height=14,
                            font=ctk.CTkFont(size=11), text_color="gray").pack(side="right")
            self.locks[key] = lock
            line = ctk.CTkFrame(self.rows, fg_color="transparent")
            line.pack(fill="x")
            var = ctk.StringVar(value=", ".join(saved.get(key, [])))
            var.trace_add("write", lambda *a: self.update_preview())
            ctk.CTkEntry(line, textvariable=var, placeholder_text="pick from list or type your own").pack(
                side="left", fill="x", expand=True)
            menu = ctk.CTkOptionMenu(line, values=options_for(key), width=34, dynamic_resizing=False,
                                     command=lambda choice, k=key, m=multi: self.pick(k, choice, m))
            menu.set("▾")
            menu.pack(side="left", padx=(4, 0))
            ctk.CTkButton(line, text="💾", width=28, fg_color="gray30",
                          command=lambda k=key: self.remember(k)).pack(side="left", padx=(4, 0))
            self.vars[key], self.menus[key] = var, menu
        self.update_preview()

    # ------------------------------------------------------------ values
    @staticmethod
    def split(text: str) -> list[str]:
        return [p.strip() for p in text.split(",") if p.strip()]

    def selections(self) -> dict[str, list[str]]:
        return {k: self.split(v.get()) for k, v in self.vars.items()}

    def pick(self, key: str, choice: str, multi: bool):
        self.menus[key].set("▾")
        cur = self.split(self.vars[key].get())
        if multi:
            if choice not in cur:
                cur.append(choice)
        else:
            cur = [choice]
        self.vars[key].set(", ".join(cur))

    def remember(self, key: str):
        text = self.vars[key].get().strip()
        if not text:
            return
        if add_option(key, text):
            self.menus[key].configure(values=options_for(key))
            messagebox.showinfo("Saved", f"Added “{text}” to your {key} list.")
        else:
            messagebox.showinfo("Already there", "That option is already in the list.")

    def has_selection(self) -> bool:
        return any(self.selections().values())

    def use_tags(self) -> bool:
        f = self.fmt.get()
        return f == "Tags" or (f == "Auto" and wants_tags(self.model))

    def compose(self, subject: str, model: dict | None = None) -> str:
        if model is not None:
            self.model = model
        return compose(subject, self.selections(), self.use_tags(), self.mode)

    def update_preview(self):
        if not self.open:
            return
        subject = self.panel.prompt.get("1.0", "end").strip() or "[your prompt]"
        text = self.compose(subject)
        style = "tags" if self.use_tags() else "sentences"
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", f"({style})  {text}")
        self.preview.configure(state="disabled")

    # ------------------------------------------------------------ actions
    def clear(self):
        for v in self.vars.values():
            v.set("")

    def insert(self):
        subject = self.panel.prompt.get("1.0", "end").strip()
        self.panel.set_prompt(self.compose(subject))
        self.clear()

    def enhance(self):
        cfg = self.panel.app.cfg
        base, mdl = cfg.get("llm_base_url", ""), cfg.get("llm_model", "")
        if not base or not mdl:
            messagebox.showinfo("LLM not set up", "Add your LLM server in Settings → Prompt LLM first.")
            self.panel.app.tabs.set("Settings")
            return
        subject = self.panel.prompt.get("1.0", "end").strip()
        sel = self.selections()
        if not subject and not any(sel.values()):
            messagebox.showinfo("Prompt", "Type an idea or pick some options first.")
            return
        m = self.panel.current_model()
        tags = self.use_tags()
        composed = compose(subject, sel, tags, self.mode)
        key = cfg.get_key("llm")
        self.prev_state = (subject, sel)
        self.llm_btn.configure(state="disabled", text="Working…")
        self.llm_status.configure(text="can take a while on phone clusters")
        events = self.panel.app.events

        def work():
            try:
                out, err = llm.enhance(base, key, mdl, subject, composed, (m or {}).get("name", ""), self.mode, tags), None
            except Exception as e:  # keep UI alive
                out, err = None, str(e)
            events.put(("ui", lambda: self.enhance_done(out, err)))
        threading.Thread(target=work, daemon=True).start()

    def enhance_done(self, out, err):
        self.llm_btn.configure(state="normal", text="✨ Enhance with LLM")
        if err:
            self.llm_status.configure(text="")
            messagebox.showerror("LLM error", err)
            return
        self.panel.set_prompt(out)
        self.clear()
        self.undo_btn.configure(state="normal")
        self.llm_status.configure(text="Prompt rewritten. Edit it freely.")

    def undo(self):
        if not self.prev_state:
            return
        subject, sel = self.prev_state
        self.panel.set_prompt(subject)
        for k, vals in sel.items():
            if k in self.vars:
                self.vars[k].set(", ".join(vals))
        self.prev_state = None
        self.undo_btn.configure(state="disabled")
        self.llm_status.configure(text="")

    def randomize(self):
        for key, _l, _m, multi, _t in CATEGORIES:
            if key not in self.vars or self.locks[key].get():
                continue
            opts = options_for(key)
            if not opts:
                continue
            if key in ("style", "lighting", "mood", "shot", "color", "camera_move"):
                pick = [random.choice(opts)]
            elif key == "negatives":
                pick = random.sample(opts, min(len(opts), random.randint(0, 3)))
            elif key in ("preserve", "audio", "motion", "quality"):
                pick = random.sample(opts, min(len(opts), random.randint(0, 2)))
            else:
                pick = [random.choice(opts)] if random.random() < 0.5 else []
            self.vars[key].set(", ".join(pick))

    # ------------------------------------------------------------ presets
    def presets(self) -> dict:
        data = storage._read_json(PRESETS_PATH, {})
        return data if isinstance(data, dict) else {}

    def refresh_presets(self):
        names = sorted(self.presets())
        self.preset_menu.configure(values=names or ["(no presets yet)"])
        self.preset_menu.set("(presets)")

    def save_preset(self):
        sel = {k: v for k, v in self.selections().items() if v}
        if not sel:
            messagebox.showinfo("Presets", "Pick some options first.")
            return
        name = ctk.CTkInputDialog(text="Name this preset:", title="Save preset").get_input()
        if not name or not name.strip():
            return
        data = self.presets()
        data[name.strip()] = {"values": sel, "format": self.fmt.get()}
        storage._write_json(PRESETS_PATH, data)
        self.refresh_presets()

    def load_preset(self, name: str):
        p = self.presets().get(name)
        if not p:
            return
        self.fmt.set(p.get("format", "Auto"))
        self.clear()
        for k, vals in p.get("values", {}).items():
            if k in self.vars:
                self.vars[k].set(", ".join(vals))
        self.preset_menu.set(name)

    def delete_preset(self):
        name = self.preset_menu.get()
        data = self.presets()
        if name in data and messagebox.askyesno("Delete preset", f"Delete “{name}”?"):
            del data[name]
            storage._write_json(PRESETS_PATH, data)
            self.refresh_presets()

    def edit_lists(self):
        from .app import open_path
        ensure_options_file()
        open_path(str(OPTIONS_PATH))
