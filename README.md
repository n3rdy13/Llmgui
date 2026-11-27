# GenStudio

A Windows desktop app for generating images and videos with your own API keys. One window gives you access to fal.ai, Replicate, OpenAI and Google models.

## Download

Get **GenStudio.exe** from the [Releases page](../../releases/tag/latest). Every push to `main` builds a fresh copy automatically. Windows SmartScreen may warn you because the exe isn't code-signed. If it does, click **More info → Run anyway**.

## Setup

1. Open the **Settings** tab and paste in a key for any provider you want to use:
   - **fal.ai** (recommended): one key covers Nano Banana, FLUX, Seedream, GPT Image, Veo 3.1, Kling 3.0, Seedance and many more. [Get a key](https://fal.ai/dashboard/keys)
   - **Replicate**: [Get a key](https://replicate.com/account/api-tokens)
   - **OpenAI**: GPT Image models. [Get a key](https://platform.openai.com/api-keys)
   - **Google**: Nano Banana and Veo through the Gemini API. [Get a key](https://aistudio.google.com/apikey)
2. Click **Save**. Keys are stored in Windows Credential Manager, not in a text file.

## Tabs

| Tab | What it does |
|---|---|
| **Image** | Text to image |
| **Edit** | Upload, paste, or send over one or more images and describe the change |
| **Video** | Text to video, or Image to video (animates a still) |
| **Gallery** | Everything you've generated. Search, reuse prompts, open files, see estimated spend |
| **Settings** | API keys, output folder, theme, model list |

- Outputs are saved to `Pictures\GenStudio\<date>\` by default.
- **→Edit / →Video** buttons send a result straight into the next step.
- **×2 / ×4** queues several runs at once. Jobs run in parallel and can be cancelled.
- **Advanced JSON** passes extra parameters to the model, e.g. `{"seed": 42, "negative_prompt": "blurry"}`. The "docs" link under each model shows every parameter it accepts.
- Costs shown are **rough estimates** for default settings. Check your provider dashboard for actual billing.

## Adding models

New models come out constantly. To add one:
- In any tab, choose **➕ Custom model…** and paste a model ID (e.g. `fal-ai/flux/dev`), or
- **Settings → Edit models.json**, add an entry, then click **Reload models**.

Each entry in `models.json` looks like this:

```json
{"provider": "fal", "mode": "i2v", "id": "fal-ai/kling-video/v3/pro/image-to-video",
 "name": "Kling 3.0 Pro", "image_param": "start_image_url", "est_usd": 1.2,
 "options": [{"key": "duration", "label": "Length", "choices": ["5", "10"]}]}
```

`mode` is one of `t2i`, `edit`, `t2v` or `i2v`. Model IDs are listed at [fal.ai/models](https://fal.ai/models) and [replicate.com/explore](https://replicate.com/explore).

## Notes

- OpenAI shut down its Sora video API in September 2026, so OpenAI is image-only here.
- Video generations take 1–5 minutes and cost real money (about $0.35–$3+ per clip on premium models).

## Run from source

Install Python 3.10+ from python.org, then double-click `run.bat`. Use `build.bat` to build the exe locally.
