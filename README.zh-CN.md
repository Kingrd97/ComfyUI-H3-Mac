# ComfyUI-H3-Mac

[English](README.md) | **简体中文**

[![tests](https://github.com/Kingrd97/ComfyUI-H3-Mac/actions/workflows/tests.yml/badge.svg)](https://github.com/Kingrd97/ComfyUI-H3-Mac/actions/workflows/tests.yml)

在 Apple Silicon Mac 上，把官方 [ComfyUI](https://github.com/Comfy-Org/ComfyUI) 可视化工作流与 MiniMax H3 Metal 后端连接起来。本项目同时支持 [antirez/h3.c](https://github.com/antirez/h3.c) 和可选的 vpipe Q8 FL2VA 后端，并提供双击安装、原生中英文节点、结构化镜头提示词、分镜合并、统一后期配音、可复用任务和 MP4 输出。

> 当前为早期版本。h3.c 本身仍在快速开发；本项目优先保证安装可重复、素材顺序明确、任务可取消、结果和日志可追踪。

## 它解决什么问题

- ComfyUI 负责拖拽编排、素材复用和参数管理。
- h3.c 负责 H3 原生权重的 Metal 推理与 MP4 编码。
- `low / auto / max` 资源档位不会暗中修改 steps、layers 或 reuse；`auto` 通常以前台友好的后台优先级慢跑，原生响应守护器或持续回退指标发现压力时短暂暂停，持续空闲后再解除后台策略。
- 节点名称、输入项、说明和悬浮提示跟随 ComfyUI 原生界面语言切换中英文。
- 六栏镜头提示词节点，以及 2–8 个镜头的无损 MP4 分镜合并。
- 每个任务独立保存 `request.json`、进度、日志、失败残片和最终视频。
- 完全相同且已完成的任务可直接复用，避免误操作后重复跑。
- 模型权重不进入 Git 仓库，下载时明确展示 MiniMax H3 许可证。

## 要求

- Apple Silicon Mac（h3.c 当前主要在 M3 Max / M5 Max 上优化和验证）
- macOS 15 或更高版本、Homebrew，以及提供 macOS SDK 26 或更高版本的 Xcode / Xcode Command Line Tools。锁定版 h3.c 使用 macOS 15 引入的运行时 Metal API 和 SDK 26 引入的编译期符号；安装器会分别预检两者，并显式设置 15.0 部署目标，不再错误继承当前 SDK 版本。
- 足够快的 SSD 和大量可用磁盘
- FL2VA 约 134 GiB；FL2VA 与 Ref2VA 两棵目录的逻辑总量约 268 GiB。锁定版本的内容寻址下载器会让相同 blob 只存一份，实际约 196 GiB，建议开始前至少留出 220 GiB；写入前会按精确版本检查空间。

48GB M5 Pro 默认推荐 `auto`。当前保守内存规则会在物理内存低于 64 GiB 时使用 h3.c 的 `--ssd-streaming`，人在使用电脑或电池供电时通常保持 macOS 后台优先级慢跑。原生 helper 会把“最近有键鼠输入 + display-link 回调间隔或回调 age 连续异常”作为强响应信号，两者同时出现时走快速暂停路径；它不需要“辅助功能”或“屏幕录制”权限，也不捕获屏幕内容。主显示器 framebuffer age 只写入诊断信息，绝不会单独触发暂停。如果 display-link 强信号不可用，还会用持续的非 H3 CPU 或 WindowServer/GPU 组合压力回退判断。`auto` 还会在严重内存、swap/pageout 或温度压力下暂停，并在低电量模式或恢复余量不足时禁止空闲满速。健康稳定 15 秒后先后台试跑 20 秒，没有复发再继续后台生成。接电、空闲 5 分钟，且最新采样中的其他 CPU、WindowServer 与显示信号都已平稳时，才解除后台策略。这些控制仍是 best-effort；`taskpolicy` 不是 GPU 硬配额。

macOS 没有可通用于任意前台 App 的真实掉帧计数接口；守护器观察的是显示系统响应，而不是读取另一个 App 的渲染器。因此原生信号和回退指标仍属于 best-effort，而不是硬实时保证：`SIGSTOP` 无法撤回已经提交给 GPU 的 Metal 工作，也不会释放模型占用的统一内存。

64 GiB 是安全启发式，不是 h3.c 的硬要求。锁定版引擎在复杂 Ref2VA 常驻示例中报告约 40.1GB 进程物理峰值，因此 48GB 再叠加浏览器、IDE 等前台应用很容易吃紧。SSD streaming 能大幅降低 DiT 常驻内存，但会进行大量只读、非缓存的模型读取，可能争抢磁盘带宽；它不会反复重写模型，也不能把读取量等同成 SSD 的写入寿命消耗。强制使用常驻模式前请先看[资源控制说明](docs/RESOURCE_CONTROL_zh.md)。

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

模型下载使用单独的锁定 Python 环境，不会改变 ComfyUI 的依赖。`runtime/models/MiniMax-H3` 是指向同一 `runtime/` 内内容寻址缓存的相对链接，整体移动项目后仍然有效。下载完成后会记录包含每个路径、尺寸、blob 标识和模型版本的清单；`Doctor.command` 会先核验清单，再调用 h3.c `--info` 检查模型结构。

配置 schema v2 使用保守的一次性升级策略：先把旧配置备份为 `config.json.v1-backup`；只有完全匹配旧版随附 `background` 默认值的配置才迁移到新的 `adaptive` 行为，用户改过的行为或阈值都会保留。

`Start.command` 会故意让 ComfyUI 控制层的 PyTorch 跑在 CPU。这**不会禁用 H3 的 Metal 推理**：H3 节点会启动单独编译的 h3.c Metal 进程。这个默认值能避免 ComfyUI 额外占用统一内存，也避免 PyTorch 设备探测失败。如果你同时使用必须依赖 MPS 的其他 ComfyUI 节点，可用 `H3_COMFY_DEVICE=auto ./Start.command` 启动。

锁定的官方 ComfyUI 前端已经原生支持中文。第一次打开时会参考浏览器语言，以后可以从 `Comfy > Locale > Language` 切换；H3 节点会跟随设置变化，不依赖第三方汉化补丁。

最简单的开始方法：打开 `工作流 > 浏览模板`，选择 `ComfyUI-H3-Mac`，载入 `H3_Beginner_2_Shot_Storyboard`。画布已经分成“参考素材、镜头 1、镜头 2、最终 MP4”四组。

## 第一个工作流

### 推荐的 vpipe Q8 工作流

已经安装 vpipe 和 Q8 FL2VA 模型时，载入 `example_workflows/H3_vpipe_Q8_2_Shot_Fixed_Voice.json`。每个 `H3 · 使用 vpipe Q8 生成` 节点根据首帧生成一个静音镜头；`H3 · 合并分镜 MP4` 负责拼接；最后由 `H3 · 添加统一固定配音` 根据每行 `秒数|台词`，用同一个音色完成整片配音。推荐的 `zh-CN-YunxiNeural` 更自然但需要联网，`macOS:Tingting` 可离线使用。“保留环境声”默认关闭，确保 H3 原人声被彻底丢弃。

完整剧情示例可载入 `example_workflows/H3_Tudou_Yunnan_8_Shot_Story_1152x640.json`：八张参考图对应八个镜头，使用 1152×640、`turbo_highres_4step` 和静音 H3 视频，合并后添加统一的“内心旁白”。猫在画面中不讲话，因此无需错误的伪口型同步；示例不添加 BGM。

vpipe 节点会优先从 `PATH` 自动查找 `vpipe`。如路径不同，可在 `config.json` 设置 `vpipe_binary`、`vpipe_work_dir`、模型、LoRA 和 low 模式的常驻内存池限制。启用这个可选后端不会改变原有 h3.c 安装路径。

在 ComfyUI 里依次添加：

1. `Load Image`：加载主体照片。
2. `H3 · 新建参考素材列表`。
3. `H3 · 添加图片参考`：连接前两个节点。
4. 如有更多素材，继续串联多个“添加参考”节点；顺序就是 Picture 1、Picture 2……
5. 推荐添加 `H3 · 编写单镜头提示词`，分栏填写分镜并把输出连到生成节点的“提示词”。
6. `H3 · 生成视频（Metal）`：连接最终参考素材，第一次用 `quality=preview`、`resource=low` 冒烟。

低内存 Mac 的普通单镜头上限是 5 秒。更长的视频应拆成多个可复用镜头，再无损合并。h3.c 的机械上限仍是 362 帧（约 15.08 秒），但超过 5 秒只会在至少 64 GiB 内存的 Mac 上放行；专家确认内存压力和磁盘余量后也可显式设置 `H3_ALLOW_LARGE_JOB=1`。这是因为长镜头 VAE 解码在内存受限机器上出现过极端 swap 增长。

确认构图正常后改成：

- `quality`：20 步、50 层、无复用，推荐正式出片。
- `reference`：50 步参考档，最慢，用于关键镜头或排查快速参数造成的差异。
- `resource=auto`：响应感知的自适应调度，通常后台慢跑，检测到响应压力时可短暂停止；低内存机器保守使用 SSD 流式加载。
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
| H3 · 使用 vpipe Q8 生成（Metal） | 调用可选 Q8 FL2VA 后端；推荐静音生成，最后统一配音 |
| H3 · 合并分镜 MP4 | 按顺序拼接 2–8 个已完成任务，不重跑 H3，也不重新压缩视频 |
| H3 · 添加统一固定配音 | 合并后按时间添加台词，全片始终使用同一个 macOS 音色 |

## 资源与画质档位

| 资源档位 | 调度/内存行为 | 是否改变 steps/layers/reuse |
|---|---|---|
| low | 使用全部核心但交给 macOS 后台调度；SSD streaming；一直慢跑 | 否 |
| auto | 进程启动时 <64 GiB 自动 streaming；使用中/电池供电时通常后台慢跑，原生响应信号或持续回退压力下暂时暂停；接电安静空闲 5 分钟后解除后台策略 | 否 |
| max | 正常优先级、无自动暂停、权重常驻；48GB 机器可能内存紧张 | 否 |

一个镜头启动后，内存路径就固定了。运行中把 `auto` 切成 `max` 只能解除后台调度，不能在去噪中途把 SSD 流式权重热切成常驻。支持的 M5 上，常驻路径还会启用 h3.c 默认的 INT8 投影，而 SSD streaming 使用原始 BF16 block；它们不会改变所选 steps/layers/reuse，但细节或构图可能存在轻微数值差异。

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

运行中可双击 `H3 Control.command` 查看状态、暂停、继续，或切换 `low / auto / max` 调度策略。命令行也可以直接使用：

```bash
./H3\ Control.command status
./H3\ Control.command pause
./H3\ Control.command resume
./H3\ Control.command auto
./H3\ Control.command max
```

“暂停/继续”使用 macOS `SIGSTOP/SIGCONT`：已加载权重和进程状态仍留在内存，继续时不会从头加载或重做已完成的 CPU 侧进度。暂停不会释放统一内存，也无法撤回已经提交给 GPU 的 Metal command buffer。它不是写入磁盘的模型检查点，因此退出 ComfyUI、杀掉进程、关机或重启后不能从同一步继续。需要把内存腾给其他应用时，应取消任务；已完成的单镜头仍会保留并可复用。详细说明见[资源调度与暂停](docs/RESOURCE_CONTROL_zh.md)。

合并后的项目保存在 `output/h3-storyboards/<storyboard-id>/`。后面的镜头失败时，前面完成的单镜头任务仍可复用。

## 为什么选 ComfyUI？Manager 是什么？

ComfyUI 是可视化节点画布、执行服务、API、队列、历史记录和工作流格式。它是目前很强的本地生成式工作流开源基础，但原始节点图并不天然等于最适合纯新手的成品软件。因此本项目保留它可靠、可复用的底座，在上面增加更小、更明确的 H3 创作层。

[ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager) 是另一个扩展，负责安装、更新、启用、禁用自定义节点和模型，并保存环境快照；它不是另一套前端。本项目不依赖它，也不默认安装，因为当前一键包锁定了验证过的版本，任意更新扩展反而容易让新手环境失去可复现性。

## 量化路线

当前锁定版 h3.c 已经会在支持的 M5 上自动选择常驻 INT8 MLP/QKV/attention 投影；SSD streaming 是独立的原始 BF16 路径，并会禁用这些常驻优化。后续界面会把“调度策略”和“内存/引擎路径”拆开，而不会把量化伪装成无损加速；只有在 48GB M5 Pro 上完成相同提示词、种子、分辨率、NFE、内存和画质对照后才调整默认值。

## 验证状态

- 自动化后端测试、shell 语法和 GitHub Actions：已验证。
- 最新版锁定 ComfyUI 的 V3 节点注册：已验证。
- 全新目录一键安装、h3.c Metal 编译、ComfyUI HTTP 启动和通过 `/object_info` 发现 H3 节点：已验证。
- 使用真实锁定 H3 快照完成生成（Ref2VA 约 196 GiB 唯一 blob）：当前版本维护者环境尚未重新下载权重验证；欢迎有权重的用户反馈结果。

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
