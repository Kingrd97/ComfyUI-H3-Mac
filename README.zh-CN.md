# ComfyUI-H3-Mac

[English](README.md) | **简体中文**

[![tests](https://github.com/Kingrd97/ComfyUI-H3-Mac/actions/workflows/tests.yml/badge.svg)](https://github.com/Kingrd97/ComfyUI-H3-Mac/actions/workflows/tests.yml)

在 Apple Silicon Mac 上，把官方 [ComfyUI](https://github.com/Comfy-Org/ComfyUI) 可视化工作流与 [antirez/h3.c](https://github.com/antirez/h3.c) 连接起来生成 MiniMax H3 视频。面向第一次接触本地视频模型的用户：双击安装、原生中英文节点、结构化镜头提示词、分镜合并和 MP4 输出。

> 当前为早期版本。h3.c 本身仍在快速开发；本项目优先保证安装可重复、素材顺序明确、任务可取消、结果和日志可追踪。

## 它解决什么问题

- ComfyUI 负责拖拽编排、素材复用和参数管理。
- h3.c 负责 H3 原生权重的 Metal 推理与 MP4 编码。
- `low / auto / max` 只控制资源调度，不偷偷降低画质。
- 节点名称、输入项、说明和悬浮提示跟随 ComfyUI 原生界面语言切换中英文。
- 六栏镜头提示词节点，以及 2–6 个镜头的无损 MP4 分镜合并。
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
4. 双击 `Start.command`，等待浏览器打开 `http://127.0.0.1:8188`。在 `Comfy > Locale > Language` 里选择“中文”。

命令行方式：

```bash
git clone https://github.com/Kingrd97/ComfyUI-H3-Mac.git
cd ComfyUI-H3-Mac
./Install.command
./Download\ Model.command
./Start.command
```

安装器会把 ComfyUI、h3.c、虚拟环境和模型放在本项目的 `runtime/` 下，便于整体移动或删除，不污染系统 Python。上游版本锁定在 `versions.env`，确保安装的是本版本已经验证过的组合。

`Start.command` 会故意让 ComfyUI 控制层的 PyTorch 跑在 CPU。这**不会禁用 H3 的 Metal 推理**：H3 节点会启动单独编译的 h3.c Metal 进程。这个默认值能避免 ComfyUI 额外占用统一内存，也避免 PyTorch 设备探测失败。如果你同时使用必须依赖 MPS 的其他 ComfyUI 节点，可用 `H3_COMFY_DEVICE=auto ./Start.command` 启动。

锁定的官方 ComfyUI 前端已经原生支持中文。第一次打开时会参考浏览器语言，以后可以从 `Comfy > Locale > Language` 切换；H3 节点会跟随设置变化，不依赖第三方汉化补丁。

最简单的开始方法：打开 `工作流 > 浏览模板`，选择 `ComfyUI-H3-Mac`，载入 `H3_Beginner_2_Shot_Storyboard`。画布已经分成“参考素材、镜头 1、镜头 2、最终 MP4”四组。

## 第一个工作流

在 ComfyUI 里依次添加：

1. `Load Image`：加载主体照片。
2. `H3 · 新建参考素材列表`。
3. `H3 · 添加图片参考`：连接前两个节点。
4. 如有更多素材，继续串联多个“添加参考”节点；顺序就是 Picture 1、Picture 2……
5. 推荐添加 `H3 · 编写单镜头提示词`，分栏填写分镜并把输出连到生成节点的“提示词”。
6. `H3 · 生成视频（Metal）`：连接最终参考素材，第一次用 `quality=preview`、`resource=low` 冒烟。

确认构图正常后改成：

- `quality`：20 步、50 层、无复用，推荐正式出片。
- `reference`：50 步参考档，最慢，用于关键镜头或排查快速参数造成的差异。
- `resource=auto`：前台友好，低内存机器自动 SSD 流式加载。
- `resource=max`：正常优先级和权重常驻内存，电脑空闲时使用。

多镜头故事给每个镜头放一组“镜头提示词 + 生成视频”，再把各生成节点的“任务目录”按顺序连接到 `H3 · 合并分镜 MP4`。完整步骤见[中文分镜教程](docs/STORYBOARD_zh-CN.md)，基础教程见 [docs/QUICKSTART_zh.md](docs/QUICKSTART_zh.md)。

## 节点

| 节点 | 用途 |
|---|---|
| H3 · 编写单镜头提示词 | 把六栏新手分镜卡组合成结构清楚的提示词 |
| H3 · 新建参考素材列表 | 创建有序素材链 |
| H3 · 添加图片参考 | 将 ComfyUI IMAGE 保存为稳定 PNG 并加入素材链 |
| H3 · 添加音频参考 | 将 ComfyUI AUDIO 保存为 WAV 并加入素材链 |
| H3 · 添加本地媒体参考 | 添加视频、带音视频、独立音轨或本地图片路径 |
| H3 · 生成视频（Metal） | 调用 h3.c，输出原生 ComfyUI VIDEO、任务目录和摘要 |
| H3 · 合并分镜 MP4 | 按顺序拼接 2–6 个已完成任务，不重跑 H3，也不重新压缩视频 |

## 资源与画质档位

| 资源档位 | 调度/内存行为 | 是否改变画质参数 |
|---|---|---|
| low | macOS 后台 QoS、nice 15、SSD streaming | 否 |
| auto | macOS 后台 QoS、nice 10；<64 GiB 自动 streaming | 否 |
| max | 正常优先级、权重常驻内存 | 否 |

| 画质 | steps | layers | reuse | 适用场景 |
|---|---:|---:|---:|---|
| preview | 4 | 50 | 1 | 快速验证提示词/构图 |
| balanced | 20 | 45 | 2 | 快速草稿 |
| quality | 20 | 50 | 1 | 正式生成 |
| reference | 50 | 50 | 1 | 最接近慢速参考 |

## 任务保存与恢复

每个请求按内容生成稳定任务 ID：

```text
output/h3-jobs/<job-id>/
├── request.json
├── progress.json
├── engine.log
├── result.partial.mp4
└── result.mp4
```

已完成的相同请求可直接复用。h3.c 目前没有导出单个去噪步状态，因此无法从第 12/20 步精确续跑；取消时会保留日志和残片，但未封装完成的 MP4 可能无法播放。

合并后的项目保存在 `output/h3-storyboards/<storyboard-id>/`。后面的镜头失败时，前面完成的单镜头任务仍可复用。

## 为什么选 ComfyUI？Manager 是什么？

ComfyUI 是可视化节点画布、执行服务、API、队列、历史记录和工作流格式。它是目前很强的本地生成式工作流开源基础，但原始节点图并不天然等于最适合纯新手的成品软件。因此本项目保留它可靠、可复用的底座，在上面增加更小、更明确的 H3 创作层。

[ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager) 是另一个扩展，负责安装、更新、启用、禁用自定义节点和模型，并保存环境快照；它不是另一套前端。本项目不依赖它，也不默认安装，因为当前一键包锁定了验证过的版本，任意更新扩展反而容易让新手环境失去可复现性。

## 验证状态

- 自动化后端测试、shell 语法和 GitHub Actions：已验证。
- 最新版锁定 ComfyUI 的 V3 节点注册：已验证。
- 全新目录一键安装、h3.c Metal 编译、ComfyUI HTTP 启动和通过 `/object_info` 发现 H3 节点：已验证。
- 使用真实 144 GB H3 权重完成生成：当前版本维护者环境尚未重新下载权重验证；欢迎有权重的用户反馈结果。

## 隐私、许可证与限制

- 所有生成和素材处理均在本机完成。
- 本仓库不包含模型、生成内容、日志或用户配置。
- 本项目代码采用 MIT License；ComfyUI、官方前端、h3.c、FFmpeg 和模型各自保留原许可证。明确的上游致谢与许可证链接见 [THIRD_PARTY.md](THIRD_PARTY.md)。
- MiniMax H3 权重受其 Community License 约束，下载前请自行阅读并确认。
- 只支持 Apple Silicon macOS。
- h3.c 要求完整原始模型目录；Ref2VA 还需要 FL2VA 基础文件。
- Ref2VA 有序参考不能与首/尾帧锚点混用。
- 当前只接 h3.c；未来可增加独立的 stable-diffusion.cpp 适配器。

## 开发

```bash
python3 -m venv .venv
.venv/bin/pip install pytest numpy pillow typing_extensions
.venv/bin/pytest -q
```

架构与扩展点见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
