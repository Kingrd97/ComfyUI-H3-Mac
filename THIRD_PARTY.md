# Third-party projects and licenses

ComfyUI-H3-Mac is a bridge and installer. It does not claim ownership of the upstream applications, inference engine, media tools, or model weights it works with.

| Project | Role | Included by this repository? | License / terms |
|---|---|---|---|
| [ComfyUI](https://github.com/Comfy-Org/ComfyUI) | Node graph, execution server, and API | Downloaded at the pinned revision during installation | [GPL-3.0](https://github.com/Comfy-Org/ComfyUI/blob/master/LICENSE) |
| [ComfyUI Frontend](https://github.com/Comfy-Org/ComfyUI_frontend) | Official web interface and native localization | Installed through ComfyUI's pinned Python requirements | [GPL-3.0](https://github.com/Comfy-Org/ComfyUI_frontend/blob/main/LICENSE) |
| [h3.c](https://github.com/antirez/h3.c) | Native MiniMax H3 inference and Metal backend | Downloaded at the pinned revision and compiled locally | [MIT](https://github.com/antirez/h3.c/blob/main/LICENSE) |
| [FFmpeg](https://ffmpeg.org/) | Media probing, encoding, and storyboard assembly | Installed through Homebrew | Upstream FFmpeg build license |
| MiniMax H3 weights | Video model data | Downloaded only after explicit user acknowledgement | MiniMax H3 Community License supplied with the model |

[ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager) is **not bundled**. It is an optional GPL-3.0 extension for installing, updating, disabling, and snapshotting custom-node environments. This project pins its own ComfyUI and h3.c revisions for reproducibility, so Manager is not required for the one-click H3 workflow.

This notice is informational and is not legal advice. Each upstream project and model retains its own copyright and license terms.
