"""API clients for each provider.

Every provider exposes run(model, prompt, images, options, extra, progress, cancel)
and returns a list of outputs: {"url": str} or {"data": bytes, "ext": str}.
"""
from __future__ import annotations

import base64
import io
import mimetypes
import time
from typing import Callable

import requests
from PIL import Image

TIMEOUT = 60          # seconds for normal HTTP calls
LONG_TIMEOUT = 300    # seconds for synchronous image calls (OpenAI, Gemini)


class ProviderError(Exception):
    pass


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- helpers

def prepare_image(path: str, max_side: int = 2048, max_bytes: int = 4_000_000) -> tuple[bytes, str]:
    """Return (bytes, mime). Downscales big images so uploads stay small."""
    with open(path, "rb") as f:
        raw = f.read()
    mime = mimetypes.guess_type(path)[0] or "image/png"
    img = Image.open(io.BytesIO(raw))
    if max(img.size) <= max_side and len(raw) <= max_bytes and mime in ("image/png", "image/jpeg", "image/webp"):
        return raw, mime
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    if img.mode in ("RGBA", "LA", "P"):
        img.convert("RGBA").save(buf, "PNG", optimize=True)
        return buf.getvalue(), "image/png"
    img.convert("RGB").save(buf, "JPEG", quality=92)
    return buf.getvalue(), "image/jpeg"


def data_uri(path: str) -> str:
    data, mime = prepare_image(path)
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _err(resp: requests.Response, prefix: str) -> ProviderError:
    try:
        j = resp.json()
        detail = j.get("detail") or j.get("error") or j
        if isinstance(detail, dict):
            detail = detail.get("message") or detail
    except Exception:
        detail = resp.text[:800]
    return ProviderError(f"{prefix} HTTP {resp.status_code}: {detail}")


def _sleep(seconds: float, cancel) -> None:
    end = time.time() + seconds
    while time.time() < end:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        time.sleep(0.2)


def find_media_urls(obj, keys=("images", "image", "video", "videos", "output", "audio")) -> list[str]:
    """Pull output URLs out of a provider response, preferring known keys."""
    found: list[str] = []

    def walk(o):
        if isinstance(o, str):
            if o.startswith("http"):
                found.append(o)
        elif isinstance(o, dict):
            if "url" in o and isinstance(o["url"], str):
                found.append(o["url"])
            else:
                for v in o.values():
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k]:
                walk(obj[k])
        if not found:
            walk(obj)
    else:
        walk(obj)
    # de-dupe, keep order
    seen, out = set(), []
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


Progress = Callable[[str], None]


# --------------------------------------------------------------------------- fal.ai

class FalProvider:
    name = "fal"

    def __init__(self, key: str):
        self.h = {"Authorization": f"Key {key}", "Content-Type": "application/json"}

    def build_payload(self, model, prompt, images, options, extra):
        payload = {}
        if prompt:
            payload[model.get("prompt_param", "prompt")] = prompt
        if images and model.get("image_param"):
            if model.get("image_list"):
                payload[model["image_param"]] = [data_uri(p) for p in images]
            else:
                payload[model["image_param"]] = data_uri(images[0])
        payload.update(options)
        payload.update(extra)
        return payload

    def run(self, model, prompt, images, options, extra, progress: Progress, cancel):
        payload = self.build_payload(model, prompt, images, options, extra)
        progress("Submitting…")
        r = requests.post(f"https://queue.fal.run/{model['id']}", json=payload, headers=self.h, timeout=TIMEOUT)
        if r.status_code >= 400:
            raise _err(r, "fal submit")
        sub = r.json()
        status_url, response_url = sub["status_url"], sub["response_url"]
        cancel_url = sub.get("cancel_url")
        interval = 3 if model.get("mode") in ("t2v", "i2v") else 1.5
        while True:
            if cancel.is_set():
                if cancel_url:
                    try:
                        requests.put(cancel_url, headers=self.h, timeout=15)
                    except Exception:
                        pass
                raise Cancelled()
            s = requests.get(status_url, headers=self.h, timeout=TIMEOUT)
            if s.status_code >= 400:
                raise _err(s, "fal status")
            sj = s.json()
            st = sj.get("status")
            if st == "IN_QUEUE":
                progress(f"In queue (position {sj.get('queue_position', '?')})")
            elif st == "IN_PROGRESS":
                progress("Generating…")
            elif st == "COMPLETED":
                break
            _sleep(interval, cancel)
        progress("Fetching result…")
        res = requests.get(response_url, headers=self.h, timeout=TIMEOUT)
        if res.status_code >= 400:
            raise _err(res, "fal result")
        urls = find_media_urls(res.json())
        if not urls:
            raise ProviderError(f"fal returned no media: {str(res.json())[:500]}")
        return [{"url": u} for u in urls]


# --------------------------------------------------------------------------- Replicate

class ReplicateProvider:
    name = "replicate"
    base = "https://api.replicate.com/v1"

    def __init__(self, key: str):
        self.h = {"Authorization": f"Bearer {key}"}

    def upload(self, path: str) -> str:
        data, mime = prepare_image(path)
        name = "input." + (mime.split("/")[-1].replace("jpeg", "jpg"))
        r = requests.post(f"{self.base}/files", headers=self.h,
                          files={"content": (name, data, mime)}, timeout=TIMEOUT)
        if r.status_code >= 400:
            raise _err(r, "Replicate upload")
        return r.json()["urls"]["get"]

    def run(self, model, prompt, images, options, extra, progress: Progress, cancel):
        inp = {}
        if prompt:
            inp[model.get("prompt_param", "prompt")] = prompt
        if images and model.get("image_param"):
            progress("Uploading image(s)…")
            urls = [self.upload(p) for p in images]
            inp[model["image_param"]] = urls if model.get("image_list") else urls[0]
        inp.update(options)
        inp.update(extra)

        mid = model["id"]
        if ":" in mid:   # owner/name:version
            url, body = f"{self.base}/predictions", {"version": mid.split(":", 1)[1], "input": inp}
        else:
            url, body = f"{self.base}/models/{mid}/predictions", {"input": inp}
        progress("Submitting…")
        r = requests.post(url, json=body, headers={**self.h, "Content-Type": "application/json"}, timeout=TIMEOUT)
        if r.status_code >= 400:
            raise _err(r, "Replicate")
        pred = r.json()
        get_url, cancel_url = pred["urls"]["get"], pred["urls"].get("cancel")
        while pred.get("status") not in ("succeeded", "failed", "canceled"):
            if cancel.is_set():
                if cancel_url:
                    try:
                        requests.post(cancel_url, headers=self.h, timeout=15)
                    except Exception:
                        pass
                raise Cancelled()
            progress("Starting (cold boot)…" if pred.get("status") == "starting" else "Generating…")
            _sleep(2.5, cancel)
            g = requests.get(get_url, headers=self.h, timeout=TIMEOUT)
            if g.status_code >= 400:
                raise _err(g, "Replicate status")
            pred = g.json()
        if pred["status"] != "succeeded":
            raise ProviderError(f"Replicate {pred['status']}: {pred.get('error')}")
        urls = find_media_urls(pred.get("output"))
        if not urls:
            raise ProviderError(f"Replicate returned no media: {str(pred.get('output'))[:500]}")
        return [{"url": u} for u in urls]


# --------------------------------------------------------------------------- OpenAI

class OpenAIProvider:
    name = "openai"
    base = "https://api.openai.com/v1"

    def __init__(self, key: str):
        self.h = {"Authorization": f"Bearer {key}"}

    def run(self, model, prompt, images, options, extra, progress: Progress, cancel):
        if model.get("mode") in ("t2v", "i2v"):
            raise ProviderError("OpenAI's video API (Sora) was shut down in September 2026. Use fal or Replicate for video.")
        params = {"model": model["id"], "prompt": prompt, **options, **extra}
        progress("Generating… (OpenAI can take up to a couple of minutes)")
        if images:
            files = []
            for p in images:
                data, mime = prepare_image(p)
                files.append(("image[]", (f"image.{mime.split('/')[-1]}", data, mime)))
            form = {k: str(v) for k, v in params.items()}
            r = requests.post(f"{self.base}/images/edits", headers=self.h, data=form, files=files, timeout=LONG_TIMEOUT)
        else:
            r = requests.post(f"{self.base}/images/generations", headers={**self.h, "Content-Type": "application/json"},
                              json=params, timeout=LONG_TIMEOUT)
        if cancel.is_set():
            raise Cancelled()
        if r.status_code >= 400:
            raise _err(r, "OpenAI")
        out = []
        fmt = str(params.get("output_format", "png")).replace("jpeg", "jpg")
        for d in r.json().get("data", []):
            if d.get("b64_json"):
                out.append({"data": base64.b64decode(d["b64_json"]), "ext": fmt})
            elif d.get("url"):
                out.append({"url": d["url"]})
        if not out:
            raise ProviderError("OpenAI returned no images.")
        return out


# --------------------------------------------------------------------------- Google (Gemini API)

class GoogleProvider:
    name = "google"
    base = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, key: str):
        self.key = key
        self.h = {"x-goog-api-key": key, "Content-Type": "application/json"}

    def run(self, model, prompt, images, options, extra, progress: Progress, cancel):
        if model.get("mode") in ("t2v", "i2v"):
            return self._veo(model, prompt, images, options, extra, progress, cancel)
        return self._image(model, prompt, images, options, extra, progress, cancel)

    def _image(self, model, prompt, images, options, extra, progress, cancel):
        parts = [{"text": prompt}]
        for p in images:
            data, mime = prepare_image(p)
            parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}})
        gen_cfg = {"responseModalities": ["TEXT", "IMAGE"]}
        img_cfg = {}
        if "aspectRatio" in options:
            img_cfg["aspectRatio"] = options["aspectRatio"]
        if "imageSize" in options:
            img_cfg["imageSize"] = options["imageSize"]
        if img_cfg:
            gen_cfg["imageConfig"] = img_cfg
        body = {"contents": [{"parts": parts}], "generationConfig": gen_cfg}
        body.update(extra)
        progress("Generating…")
        r = requests.post(f"{self.base}/models/{model['id']}:generateContent", headers=self.h, json=body, timeout=LONG_TIMEOUT)
        if cancel.is_set():
            raise Cancelled()
        if r.status_code >= 400:
            raise _err(r, "Google")
        j = r.json()
        out, texts = [], []
        for cand in j.get("candidates", []):
            for part in (cand.get("content") or {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline:
                    mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                    out.append({"data": base64.b64decode(inline["data"]),
                                "ext": mime.split("/")[-1].replace("jpeg", "jpg")})
                elif part.get("text"):
                    texts.append(part["text"])
        if not out:
            reason = j.get("promptFeedback") or [c.get("finishReason") for c in j.get("candidates", [])]
            raise ProviderError(f"Google returned no image. {' '.join(texts)[:300]} {reason}")
        return out

    def _veo(self, model, prompt, images, options, extra, progress, cancel):
        instance = {"prompt": prompt}
        if images:
            data, mime = prepare_image(images[0])
            instance["image"] = {"bytesBase64Encoded": base64.b64encode(data).decode(), "mimeType": mime}
        body = {"instances": [instance], "parameters": {**options, **extra}}
        if not body["parameters"]:
            del body["parameters"]
        progress("Submitting…")
        r = requests.post(f"{self.base}/models/{model['id']}:predictLongRunning", headers=self.h, json=body, timeout=TIMEOUT)
        if r.status_code >= 400:
            raise _err(r, "Google Veo")
        op_name = r.json()["name"]
        started = time.time()
        while True:
            _sleep(8, cancel)
            g = requests.get(f"{self.base}/{op_name}", headers=self.h, timeout=TIMEOUT)
            if g.status_code >= 400:
                raise _err(g, "Google Veo status")
            op = g.json()
            if op.get("done"):
                break
            progress(f"Generating… {int(time.time() - started)}s")
        if op.get("error"):
            raise ProviderError(f"Veo error: {op['error'].get('message', op['error'])}")
        resp = op.get("response", {})
        samples = (resp.get("generateVideoResponse") or {}).get("generatedSamples") or resp.get("generatedVideos") or []
        out = []
        progress("Downloading…")
        for s in samples:
            uri = (s.get("video") or {}).get("uri")
            if uri:
                d = requests.get(uri, headers={"x-goog-api-key": self.key}, timeout=LONG_TIMEOUT, allow_redirects=True)
                if d.status_code >= 400:
                    raise _err(d, "Veo download")
                out.append({"data": d.content, "ext": "mp4"})
        if not out:
            raise ProviderError(f"Veo returned no video (possibly blocked by safety filters): {str(resp)[:400]}")
        return out


PROVIDERS = {
    "fal": FalProvider,
    "replicate": ReplicateProvider,
    "openai": OpenAIProvider,
    "google": GoogleProvider,
}

PROVIDER_LABELS = {"fal": "fal.ai", "replicate": "Replicate", "openai": "OpenAI", "google": "Google"}

KEY_URLS = {
    "fal": "https://fal.ai/dashboard/keys",
    "replicate": "https://replicate.com/account/api-tokens",
    "openai": "https://platform.openai.com/api-keys",
    "google": "https://aistudio.google.com/apikey",
}


def test_key(provider: str, key: str) -> tuple[bool, str]:
    """Cheap, free call to check a key works."""
    try:
        if provider == "fal":
            ok = ":" in key and len(key) > 20
            return ok, ("Format looks right. fal keys are verified on the first generation."
                        if ok else "fal keys look like 'id:secret'.")
        if provider == "replicate":
            r = requests.get("https://api.replicate.com/v1/account", headers={"Authorization": f"Bearer {key}"}, timeout=20)
        elif provider == "openai":
            r = requests.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
        elif provider == "google":
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
                             headers={"x-goog-api-key": key}, timeout=20)
        else:
            return False, "Unknown provider"
        if r.status_code < 400:
            return True, "Key works."
        return False, str(_err(r, ""))
    except Exception as e:
        return False, f"Connection error: {e}"
