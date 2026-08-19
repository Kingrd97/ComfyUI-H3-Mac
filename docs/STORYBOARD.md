# Storyboard workflow

[简体中文](STORYBOARD_zh-CN.md) | **English**

This workflow is for creators who want to think in shots without learning every ComfyUI concept first.

## Switch the interface language

ComfyUI has native localization. Open `Comfy > Locale > Language` and choose English or Chinese. H3 node names, fields, descriptions, and tooltips follow the same setting after this extension is installed.

## The six-box shot card

Add `H3 · Build shot prompt` and fill in:

1. **Subject and continuity** — identity, clothing/fur, proportions, and traits that must stay stable.
2. **Action timeline** — visible actions in chronological segments such as `0–2s` and `2–5s`.
3. **Environment and physical interaction** — location, water, wind, props, contact, and reactions.
4. **Camera and framing** — wide/medium/close, angle, movement, and whether this is one take.
5. **Look, lighting, and sound** — realism, mood, dialogue, and ambient audio.
6. **Avoid** — frozen poses, identity drift, anatomy errors, unwanted cuts, text, and watermark.

Connect its output to `Prompt` on `H3 · Generate video (Metal)`.

## A three-shot example

Plan each shot as a separate prompt and generation node. Reuse the same ordered reference chain when subject identity must stay consistent.

| Shot | Duration | Purpose | Example action |
|---|---:|---|---|
| 1 | 3s | Establish | Wide view: the cat steps into the shallow forest stream and looks around. |
| 2 | 4s | Main action | Medium tracking shot: the cat splashes repeatedly with both front paws and follows the current. |
| 3 | 3s | Payoff | Close-up: wet whiskers, one final playful splash, then the cat looks at camera. |

Use identical width, height, and quality settings for every shot. Connect each generator's `Job directory` output to Shot 1, Shot 2, and Shot 3 on `H3 · Assemble storyboard MP4`. The assembler makes hard cuts and copies the existing media streams, so it is fast and does not reduce image quality.

## Draft, approve, finish

1. Make every shot 2–3 seconds with `preview + low`.
2. Approve subject identity, action direction, and camera separately.
3. Keep the chosen seed and rewrite only the weak field.
4. Switch approved shots to their final duration and `quality + auto`.
5. Assemble the completed jobs. If a later shot fails or is cancelled, completed shots are reused on the next run.

The assembled project is stored under `ComfyUI/output/h3-storyboards/<storyboard-id>/`. Individual shot requests, logs, and videos remain under `ComfyUI/output/h3-jobs/`.

## Important limits

- The assembler currently uses hard cuts, not transitions or subtitles.
- All shots should use the same width, height, FPS, and compatible codecs.
- ComfyUI organizes the workflow; h3.c still performs all H3 inference through Metal.
- A completed denoising shot is reusable, but an interrupted shot cannot resume from an individual denoising step.
