# Third-party projects and licenses

ComfyUI-H3-Mac is a bridge and installer. It does not claim ownership of the upstream applications, inference engine, media tools, or model weights it works with.

| Project | Role | Included by this repository? | License / terms |
|---|---|---|---|
| [ComfyUI](https://github.com/Comfy-Org/ComfyUI) | Node graph, execution server, and API | Downloaded at the pinned revision during installation | [GPL-3.0](https://github.com/Comfy-Org/ComfyUI/blob/master/LICENSE) |
| [ComfyUI Frontend](https://github.com/Comfy-Org/ComfyUI_frontend) | Official web interface and native localization | Installed through ComfyUI's pinned Python requirements | [GPL-3.0](https://github.com/Comfy-Org/ComfyUI_frontend/blob/main/LICENSE) |
| [h3.c](https://github.com/antirez/h3.c) | Native MiniMax H3 inference and Metal backend | Downloaded at the pinned revision and compiled locally | [MIT](https://github.com/antirez/h3.c/blob/main/LICENSE) |
| [vpipe](https://github.com/tgo-app-dev/vpipe) | Q8 MiniMax H3 Metal backend and model tooling | The official signed app bundle is downloaded at the pinned release and copied intact into `runtime/` | [Apache-2.0](https://github.com/tgo-app-dev/vpipe/blob/main/LICENSE) |
| [FFmpeg](https://ffmpeg.org/) | Media probing, encoding, and storyboard assembly | Installed through Homebrew | Upstream FFmpeg build license |
| [py-lmdb](https://github.com/jnwatson/py-lmdb) | Read-only validation of vpipe's local model registry | Installed into ComfyUI's Python environment from the pinned requirement | [OpenLDAP Public License](https://github.com/jnwatson/py-lmdb/blob/master/LICENSE) |
| MiniMax H3 weights | Video model data | Downloaded only after explicit user acknowledgement | MiniMax H3 Community License supplied with the model |
| [MiniMax-H3-Turbo-LoRA](https://huggingface.co/larryvrh/MiniMax-H3-Turbo-Lora) | 544p four/six-step preview and production adapter | Downloaded at a pinned immutable revision by the Q8 preparation command | Model repository terms |
| [Minimax-h3-Turbo](https://huggingface.co/lightx2v/Minimax-h3-Turbo) | 768p four-step high-resolution adapter | Downloaded at a pinned immutable revision by the Q8 preparation command | Model repository terms |

[ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager) is **not bundled**. It is an optional GPL-3.0 extension for installing, updating, disabling, and snapshotting custom-node environments. This project pins its own ComfyUI and h3.c revisions for reproducibility, so Manager is not required for the one-click H3 workflow.

This notice is informational and is not legal advice. Each upstream project and model retains its own copyright and license terms.
