"""Talk to any OpenAI-compatible chat server (PrivateLM, llama.cpp, LM Studio, Ollama, OpenAI...)."""
from __future__ import annotations

import re

import requests

TIMEOUT = 300  # local / phone-cluster models can be slow


class LLMError(Exception):
    pass


def _base(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url:
        raise LLMError("Set the LLM Base URL in Settings first.")
    if not re.match(r"^https?://", url):
        url = "http://" + url
    return url


def _headers(key: str) -> dict:
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def list_models(base_url: str, key: str) -> list[str]:
    try:
        r = requests.get(_base(base_url) + "/models", headers=_headers(key), timeout=15)
    except requests.RequestException as e:
        raise LLMError(f"Can't reach {base_url}: {e}") from e
    if r.status_code >= 400:
        raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    items = data.get("data") or data.get("models") or []
    return [m.get("id") or m.get("name") or m.get("model") for m in items if isinstance(m, dict)]


def chat(base_url: str, key: str, model: str, system: str, user: str,
         temperature: float = 0.7, max_tokens: int = 500) -> str:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    try:
        r = requests.post(_base(base_url) + "/chat/completions", json=body, headers=_headers(key), timeout=TIMEOUT)
    except requests.RequestException as e:
        raise LLMError(f"Can't reach the LLM server: {e}") from e
    if r.status_code >= 400:
        raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:400]}")
    try:
        text = r.json()["choices"][0]["message"]["content"] or ""
    except Exception:
        raise LLMError(f"Unexpected LLM response: {r.text[:300]}")
    return clean(text)


def clean(text: str) -> str:
    """Strip reasoning blocks, code fences, labels and wrapping quotes some models add."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    text = re.sub(r"<\|?channel\|?>.*?<\|?channel\|?>", "", text, flags=re.S)
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    text = re.sub(r"^(here(?:'s| is)[^:\n]*:|prompt:|enhanced prompt:|final prompt:)\s*", "", text, flags=re.I).strip()
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'“”":
        text = text[1:-1].strip()
    return text


MODE_HINT = {
    "t2i": "a text-to-image model",
    "edit": "an image-editing model. The prompt is an instruction describing the change to make to the supplied image",
    "t2v": "a text-to-video model. Describe the subject, the action over time, camera movement and (if relevant) sound",
    "i2v": "an image-to-video model that animates a supplied still image. Describe only the motion, camera movement and (if relevant) sound, not what the image already shows",
}


def system_prompt(model_name: str, mode: str, tags: bool) -> str:
    style = ("a single line of comma-separated descriptive tags and short phrases"
             if tags else "one or two natural-language paragraphs written as plain descriptive sentences")
    return (
        "You are an expert prompt writer for AI image and video generators. "
        f"You will write a prompt for {model_name or 'a generative model'}, which is {MODE_HINT.get(mode, 'a generative model')}.\n"
        f"Write {style}.\n"
        "Rules:\n"
        "- Keep the user's subject and intent exactly; never change who or what the prompt is about.\n"
        "- Work every option the user picked into the prompt naturally. Be concrete and visual: materials, textures, "
        "light, color, framing, atmosphere.\n"
        "- If the draft contains an 'Avoid:' list, phrase the prompt so those things are absent and end with a short 'Avoid: ...' clause.\n"
        "- Do not invent text to be rendered in the image, brand names, or real people.\n"
        f"- Keep it under {'60' if tags else '150'} words.\n"
        "- Output ONLY the finished prompt. No preamble, no quotation marks, no explanation, no markdown."
    )


def enhance(base_url: str, key: str, model: str, subject: str, composed: str,
            target_model: str, mode: str, tags: bool) -> str:
    user = f"User's idea: {subject or '(none given, invent a fitting subject from the options)'}\n\n"
    if composed.strip() and composed.strip() != subject.strip():
        user += f"Draft built from their option picks:\n{composed}\n\n"
    user += "Write the final prompt now."
    out = chat(base_url, key, model, system_prompt(target_model, mode, tags), user)
    if not out:
        raise LLMError("The LLM returned an empty reply. Try again.")
    return out
