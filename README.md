# ComfyUI-H3-Mac

在 Apple Silicon Mac 上，用 ComfyUI 可视化工作流驱动 [antirez/h3.c](https://github.com/antirez/h3.c) 生成 MiniMax H3 视频。面向第一次接触本地视频模型的用户：双击安装、双击下载模型、双击启动，默认输出 MP4。

> 当前为早期版本。h3.c 本身仍在快速开发；本项目优先保证安装可重复、素材顺序明确、任务可取消、结果和日志可追踪。

## 它解决什么问题

- ComfyUI 负责拖拽编排、素材复用和参数管理。
- h3.c 负责 H3 原生权重的 Metal 推理与 MP4 编码。
- `low / auto / max` 只控制资源调度，不偷偷降低画质。
- 每个任务独立保存 `request.json`、进度、日志、失败残片和最终视频。
- 完全相同且已完成的任务可直接复用，避免误操作后重复跑。
- 模型权重不进入 Git 仓库，下载时明确展示 MiniMax H3 许可证。

## 要求

- Apple Silicon Mac（h3.c 当前主要在 M3 Max / M5 Max 上优化和验证）
- macOS、Homebrew、Xcode Command Line Tools
- 足够快的 SSD 和大量可用磁盘
- Ref2VA 套件约 144 GB，建议至少预留 170 GB

普通内存容量的 Mac 建议先用 `low` 或 `auto`。这两个模式会使用 h3.c 的 `--ssd-streaming`（`auto` 在内存小于 64 GiB 时自动启用），以速度换内存；同一权重和参数下不会主动降低画质。

## 一键安装

1. 下载或克隆本仓库。
2. 双击 `Install.command`。macOS 首次拦截时，用右键 → 打开；不要关闭系统安全保护。
3. 双击 `Download Model.command`，新手选择 `1) Ref2VA`。
4. 双击 `Start.command`，等待浏览器打开 `http://127.0.0.1:8188`。

命令行方式：

```bash
git clone https://github.com/Kingrd97/ComfyUI-H3-Mac.git
cd ComfyUI-H3-Mac
./Install.command
./Download\ Model.command
./Start.command
```

安装器会把 ComfyUI、h3.c、虚拟环境和模型放在本项目的 `runtime/` 下，便于整体移动或删除，不污染系统 Python。
上游版本锁定在 `versions.env`，确保安装的是本版本已经验证过的组合，而不是不可预测的未来主分支。

## 第一个工作流

在 ComfyUI 里依次添加：

1. `Load Image`：加载主体照片。
2. `H3 · 新建参考素材列表`。
3. `H3 · 添加图片参考`：连接前两个节点。
4. 如有更多素材，继续串联多个“添加参考”节点；顺序就是 Picture 1、Picture 2……
5. `H3 · 生成视频（Metal）`：连接最终参考素材，填写提示词，第一次用 `quality=preview`、`resource=low` 冒烟。

确认构图正常后改成：

- `quality`：20 步、50 层、无复用，推荐正式出片。
- `reference`：50 步参考档，最慢，用于关键镜头或排查快速参数造成的差异。
- `resource=auto`：前台友好，低内存机器自动 SSD 流式加载。
- `resource=max`：不做后台 QoS，也不启用 SSD 流式加载，内存足够且电脑空闲时使用。

更完整的中文教程见 [docs/QUICKSTART_zh.md](docs/QUICKSTART_zh.md)。

## 节点

| 节点 | 用途 |
|---|---|
| H3 · 新建参考素材列表 | 创建有序素材链 |
| H3 · 添加图片参考 | 将 ComfyUI IMAGE 保存为稳定 PNG 并加入素材链 |
| H3 · 添加音频参考 | 将 ComfyUI AUDIO 保存为 WAV 并加入素材链 |
| H3 · 添加本地媒体参考 | 添加视频、带音视频、独立音轨或本地图片路径 |
| H3 · 生成视频（Metal） | 调用 h3.c，输出原生 ComfyUI VIDEO、任务目录和摘要 |

## 资源与画质档位

| 档位 | 调度/内存行为 | 画质参数 |
|---|---|---|
| low | macOS 后台 QoS、nice 15、SSD streaming | 不改 |
| auto | macOS 后台 QoS、nice 10；<64 GiB 自动 streaming | 不改 |
| max | 正常优先级、权重常驻内存 | 不改 |

| 画质 | steps | layers | reuse | 适用场景 |
|---|---:|---:|---:|---|
| preview | 4 | 50 | 1 | 快速验证提示词/构图 |
| balanced | 20 | 45 | 2 | 快速草稿 |
| quality | 20 | 50 | 1 | 正式生成 |
| reference | 50 | 50 | 1 | 最接近慢速参考 |

## 任务保存与恢复

每个请求按内容生成稳定任务 ID，文件位于 ComfyUI 的：

```text
output/h3-jobs/<job-id>/
├── request.json
├── progress.json
├── engine.log
├── result.partial.mp4
└── result.mp4
```

如果 `result.mp4` 已完成且开启“复用相同的已完成任务”，再次运行会直接返回。h3.c 目前没有导出单个去噪步状态，因此无法从第 12/20 步精确续跑；取消时会保留日志和残片，后续可据此诊断，但未封装完成的 MP4 可能无法播放。

## 隐私与许可证

- 所有生成和素材处理均在本机完成。
- 本仓库不包含模型、生成内容、日志或用户配置；`.gitignore` 会阻止常见大权重和运行目录被提交。
- 本项目代码采用 MIT License。
- ComfyUI、h3.c、FFmpeg 各自保留原许可证。
- MiniMax H3 权重受其 Community License 约束，下载前请自行阅读并确认。

## 已知限制

- 只支持 Apple Silicon macOS。
- h3.c 要求完整的原始目录结构；Ref2VA 还需要 FL2VA 基础文件。
- Ref2VA 有序参考不能与首/尾帧锚点混用。
- 本版本的“本地媒体参考”使用文件路径；后续版本会加入原生 ComfyUI VIDEO 上传/裁剪落盘节点。
- 当前只接 h3.c。后端层已独立，未来可以增加 stable-diffusion.cpp 适配器，但不会把两种引擎伪装成完全相同的能力。

## 开发

```bash
python3 -m venv .venv
.venv/bin/pip install pytest numpy pillow typing_extensions
.venv/bin/pytest -q
```

架构与扩展点见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
