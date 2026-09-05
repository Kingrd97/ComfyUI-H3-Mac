# ComfyUI-H3-Mac

[English](README.md) | **简体中文**

[![tests](https://github.com/Kingrd97/ComfyUI-H3-Mac/actions/workflows/tests.yml/badge.svg)](https://github.com/Kingrd97/ComfyUI-H3-Mac/actions/workflows/tests.yml)

在 Apple Silicon Mac 上，把官方 [ComfyUI](https://github.com/Comfy-Org/ComfyUI) 可视化工作流与 MiniMax H3 Metal 后端连接起来。在 macOS 26 上会安装经过固定版本与签名校验的 [vpipe](https://github.com/tgo-app-dev/vpipe) 运行时，用户可选择 vpipe Q8 FL2VA 权重，也可完全不下载 Q8，改用 [antirez/h3.c](https://github.com/antirez/h3.c) 运行 MiniMax H3 官方原始 BF16 权重。项目提供双击安装、原生中英文节点、结构化镜头提示词、分镜合并、统一后期配音、可复用任务和 MP4 输出。

> 当前为早期版本。h3.c 本身仍在快速开发；本项目优先保证安装可重复、素材顺序明确、任务可取消、结果和日志可追踪。

## 它解决什么问题

- ComfyUI 负责拖拽编排、素材复用和参数管理。
- 固定版本的官方 vpipe，以及适合 24/48GB Mac、可续传并严格校验的 Q8 准备流程。
- h3.c 负责 H3 原生权重的 Metal 推理与 MP4 编码。
- `low / auto / max` 资源档位不会暗中修改 steps、layers 或 reuse；`auto` 通常以前台友好的后台优先级慢跑，原生响应守护器或持续回退指标发现压力时短暂暂停，持续空闲后再解除后台策略。
- 节点名称、输入项、说明和悬浮提示跟随 ComfyUI 原生界面语言切换中英文。
- 六栏镜头提示词节点，以及 2–8 个镜头的无损 MP4 分镜合并。
- 每个任务独立保存 `request.json`、进度、日志、失败残片和最终视频。
- 使用用户级 launchd 同时保活 ComfyUI 和持久化 vpipe 队列 worker；vpipe 镜头由 worker 持有，关闭或重启 ComfyUI 不会杀死 Metal 推理进程。
- 完全相同且已完成的任务可直接复用，避免误操作后重复跑。
- 模型权重不进入 Git 仓库，下载时明确展示 MiniMax H3 许可证。

## 要求

- Apple Silicon Mac。推荐的 vpipe 路线要求 **macOS 26 或更高版本**；安装器会拒绝不兼容二进制，不会勉强运行。h3.c 本身支持 macOS 15+，但锁定源码仍需要带 macOS SDK 26 的 Xcode / Command Line Tools。
- Homebrew 和足够快的 SSD。
- 推荐 vpipe Q8：最终 Q8 约 65 GiB，另有两套 Turbo LoRA 约 2.5 GiB；首次紧凑转换需约 120 GiB 可用空间。每个 BF16 临时阶段校验后立即删除，Ctrl-C 后重跑可续传。
- 高级 h3.c BF16：FL2VA 约 134 GiB；FL2VA + Ref2VA 通过内容寻址缓存实际约 196 GiB，分别建议预留 150/220 GiB。

48GB M5 Pro 默认推荐 vpipe Q8 + `auto`，不需要走 h3.c 原始权重的 SSD streaming。人在使用电脑或电池供电时通常保持 macOS 后台优先级慢跑；原生 helper 把“最近有键鼠输入 + display-link 回调间隔或 age 连续异常”作为强响应信号，两者同时出现时快速暂停。它不需要辅助功能或屏幕录制权限，也不捕获屏幕。强信号不可用时会根据持续的其他 CPU、WindowServer/GPU 压力回退判断；严重内存、swap/pageout 或温度压力也会暂停。恢复健康后先后台试跑，接电并安静空闲五分钟后再解除后台策略。这些控制仍是 best-effort；`taskpolicy` 不是 GPU 硬配额。

macOS 没有可通用于任意前台 App 的真实掉帧计数接口；守护器观察的是显示系统响应，而不是读取另一个 App 的渲染器。因此原生信号和回退指标仍属于 best-effort，而不是硬实时保证：`SIGSTOP` 无法撤回已经提交给 GPU 的 Metal 工作，也不会释放模型占用的统一内存。

对于高级 h3.c 路线，64 GiB 只是安全启发式。锁定版引擎在复杂 Ref2VA 常驻示例中报告约 40.1GB 进程物理峰值，48GB 再叠加前台应用会很紧。SSD streaming 会降低 DiT 常驻内存，但也会进行大量只读模型读取、争抢磁盘带宽。强制常驻前请先看[资源控制说明](docs/RESOURCE_CONTROL_zh.md)。

## 先选择一条模型路线

本项目有两条独立的推理链。“使用 ComfyUI”不等于必须使用 vpipe：

```text
原始 BF16：ComfyUI → H3 · 生成视频（Metal）节点 → h3.c → MiniMax H3 FL2VA/Ref2VA BF16
量化 Q8： ComfyUI → H3 · 使用 vpipe Q8 FL2VA/Ref2VA 生成节点 → launchd worker → vpipe Q8
```

| 下载选项 | 后端与权重 | 适用能力 | 磁盘需求 | 适合谁 |
|---|---|---|---:|---|
| `1) vpipe Q8 FL2VA` | vpipe + Q8 | 首帧 FL2VA；如需有序图片/视频/音频参考，可再准备独立 Ref2VA Q8 包 | 首次需约 120 GiB，完成后约 67.5 GiB，不含可选 Ref2VA | 24/48GB Mac 的速度/空间优先路线 |
| `2) h3.c Ref2VA BF16` | h3.c + 官方原始 BF16 | 有序多图、视频和音频参考；会自动包含必需的 FL2VA 基础 | 实际约 196 GiB，下载前建议至少 220 GiB 可用 | **48GB M5 Pro 想要原始权重与多参考时选这个** |
| `3) h3.c FL2VA BF16` | h3.c + 官方原始 BF16 | 文生视频或首/尾帧锚定；不能接有序多参考 | 约 134 GiB，建议至少 150 GiB 可用 | 明确不需要 Ref2VA 的高级用户 |

选项 2 会直接下载 Ref2VA 和必需的 FL2VA，**不需要先选 3，也不需要下载或转换 Q8**。Q8 和 BF16 是可并存但不互相替代的两套资产；同时安装会叠加占用磁盘。

这里的“原始 BF16”指磁盘上保存 MiniMax 官方 BF16 checkpoint。48GB 机器上 `auto`/`low` 默认使用 SSD streaming 的原始 BF16 block；`max` 常驻路径在支持的 M5 上可能使用 h3.c 自带的 INT8 MLP/QKV/attention 投影核，所以不应将“原始权重文件”理解成所有运行路径都是纯 BF16 算术。

### 48GB M5 Pro 原始权重：可直接交给 GPT 执行的安装协议

```bash
git clone https://github.com/Kingrd97/ComfyUI-H3-Mac.git
cd ComfyUI-H3-Mac
./Install.command
./Download\ Model.command Ref2VA
# 用户本人阅读 MiniMax H3 许可证后，在提示时输入 AGREE
./Doctor.command
./Start.command
```

给执行安装的 GPT / 编码助手的约束：

- 先确认 `uname -m` 是 `arm64`，`xcrun --sdk macosx --show-sdk-version` 为 26 或更高，`brew --version` 可用，`df -h .` 显示至少 220 GiB 可用。缺 Command Line Tools 时由用户运行 `xcode-select --install`；Homebrew 只从 [brew.sh](https://brew.sh/) 安装。
- 在用户确认项目的最终目录后再下载大权重；不要安装到助手的临时沙盒。
- 参数必须写 `Ref2VA`，不是数字 `2`。GPT 不应替用户接受模型许可证；应暂停并请用户本人输入 `AGREE`。
- 不要运行 `Prepare vpipe Q8.command`，不要为了消除 vpipe worker 的 Q8 提示而下载额外权重，不要擅自修改 `auto_ssd_streaming_ram_gib`或默认用 `max`。
- 下载可 Ctrl-C 中断；重跑同一条 `./Download\ Model.command Ref2VA` 会复用内容寻址缓存。不要删除 `runtime/models/.cache`或未完成的 blob。
- `Doctor.command` 返回 0，并显示 FL2VA、Ref2VA、锁定清单、h3.c `--info` 和 H3 节点就绪，才算安装完成。不要通过盲删 `runtime/`重来解决错误。
- 安装和下载需要联网；权重、依赖和本地音色就绪后，BF16 视频生成可以断网。只有显式选择 Neural 语音服务时才会把台词发送到网络服务。
- 本地文生/图生视频不需要 Full Disk Access、Camera、Microphone、Accessibility 或 Screen Recording。只在用户明确要读受保护目录、摄像头或麦克风时再授予相应权限。ComfyUI 默认只监听 `127.0.0.1`。

## 一键安装

1. 下载或克隆本仓库。
2. 双击 `Install.command`。macOS 首次拦截时，用右键 → 打开；不要关闭系统安全保护。
3. 双击 `Download Model.command`，按上表只选一条路线：速度/空间优先选 `1`；48GB M5 Pro 想用原始权重和多参考选 `2`。
4. 运行 `Doctor.command`。BF16-only 路线不会生成 Q8 资产；如果 vpipe worker 显示 `degraded / Waiting for vpipe assets`，这是预期状态，不影响 `H3 · 生成视频（Metal）` 的 h3.c BF16 推理。
5. 安装器会启动并保活 ComfyUI 与 vpipe worker。双击 `Start.command` 可检查服务并打开 `http://127.0.0.1:8188`。在 `Comfy > Locale > Language` 里选择“中文”。

命令行方式：

```bash
git clone https://github.com/Kingrd97/ComfyUI-H3-Mac.git
cd ComfyUI-H3-Mac
./Install.command
./Download\ Model.command
./Doctor.command
./Start.command
```

全新安装时，ComfyUI、固定版本的 vpipe 应用包、h3.c、虚拟环境、vpipe 工作目录和模型都位于本项目的 `runtime/` 下。请尽量先确定项目最终位置再下载大权重。如果之后整体移动了目录：

- BF16-only：重跑 `Install.command`，再运行 `Doctor.command`。项目内的相对内容寻址链接会继续复用完整权重，不需要重下。
- vpipe Q8：重跑 `Install.command`，再运行 `Prepare vpipe Q8.command low`，刷新 launchd 路径、软链接和 vpipe 模型注册表。

上游版本和官方 vpipe DMG 校验值锁定在 `versions.env`。macOS 26 上 `Install.command` 仍会安装小型 vpipe 运行时并启动 worker，即使只用 BF16；这不代表已下载约 67.5 GiB 的 Q8 模型。

模型下载使用单独的锁定 Python 环境，不会改变 ComfyUI 的依赖。`runtime/models/MiniMax-H3` 是指向同一 `runtime/` 内内容寻址缓存的相对链接，整体移动项目后仍然有效。下载完成后会记录包含每个路径、尺寸、blob 标识和模型版本的清单；`Doctor.command` 会先核验清单，再调用 h3.c `--info` 检查模型结构。

配置 schema v4 使用保守迁移策略：先备份旧配置，保留用户改过的资源阈值和仍存在的外 vpipe 工作目录；旧的未锁定 `vpipe` 可执行默认值会迁到项目内已验证的二进制。

升级现有安装时，先等待 BF16 镜头完成或取消，不要在正在推理时重启服务。然后执行：

```bash
git status --short       # 先确认没有未保存的个人修改
git pull --ff-only
./Install.command
./Doctor.command
./Start.command
```

已完整的锁定权重通常会直接复用；只在 `Doctor.command` 明确报告模型 revision/清单不匹配时，才重跑相同的下载命令。

`Start.command` 只会安装/检查 launchd 服务并打开界面，不会重复启动已存在的服务器。launchd 中的 ComfyUI 控制层故意让 PyTorch 跑在 CPU；这**不会禁用 Metal 推理**，H3 节点会调用独立的 Metal 引擎。前台诊断时可使用 `H3_FOREGROUND=1 H3_COMFY_DEVICE=auto ./Start.command`。

锁定的官方 ComfyUI 前端已经原生支持中文。第一次打开时会参考浏览器语言，以后可以从 `Comfy > Locale > Language` 切换；H3 节点会跟随设置变化，不依赖第三方汉化补丁。

完成模型选项 1 后，最简单的开始方法是打开 `工作流 > 浏览模板`，选择 `ComfyUI-H3-Mac`，载入 `H3_vpipe_Q8_2_Shot_Fixed_Voice`。画布已经分成“参考素材、镜头 1、镜头 2、最终 MP4、统一旁白”几组。完成模型选项 2 后，载入 `H3_Beginner_2_Shot_Storyboard`；它使用 h3.c BF16/Ref2VA，默认 `preview + auto`，适合 48GB M5 Pro 首次保守冒烟。

## 第一个工作流

### 推荐的 vpipe Q8 工作流

模型选项 1 完成后，载入 `example_workflows/H3_vpipe_Q8_2_Shot_Fixed_Voice.json`。每个 `H3 · 使用 vpipe Q8 生成` 节点根据首帧生成静音镜头；合并后再按每行 `秒数|台词` 统一配音。公开模板默认使用完全离线的 `macOS:Tingting`；Neural 音色为显式联网选项，会把台词发送到对应语音服务。“保留环境声”默认关闭，避免多个镜头的原人声与最终旁白叠加。

本地 Q8 多参考任务需先完成 FL2VA，再运行 `./Prepare\ vpipe\ Ref2VA\ Q8.command low`。用图片/音频/视频节点建立有序素材链，并连接到 `H3 · 使用 vpipe Q8 多参考生成（Metal）`。音频不能是唯一参考。节点默认 `low`、8 步、参考图短边 1024，优先保证 24/48GB Mac 上首次使用可控。

节点会优先使用项目内经过验证的 vpipe，并把持久化任务票据提交给 launchd worker。Q8 与两套固定版本 LoRA 未通过完整性校验前，worker 会明确显示 `degraded/等待资产`，不会到 0% 后才模糊失败。高级用户仍可在 `config.json` 覆盖路径和 low/auto 的内存池限制。

手动搭建时，最短的 vpipe 管线是：

1. `Load Image`：加载该镜头的首帧图。
2. 可选 `H3 · 编写单镜头提示词`，把文本输出连到生成节点。
3. `H3 · 使用 vpipe Q8 生成（Metal）`，把图片连到“首帧参考”。
4. 多镜头时重复“提示词 + 首帧 + vpipe 生成”，把每个“任务目录”按时间线连到 `H3 · 合并分镜 MP4`。
5. 如需旁白，合并后再加 `H3 · 添加统一固定配音`，避免各镜头声线不一致。

公开模板默认是 `960×544`、124 帧、6 步 Turbo、静音生成和 `resource=auto`，适合首次冒烟。`auto` 在使用电脑时保持后台友好，持续响应压力下自动暂停，接电且持续空闲后恢复正常优先级。`max` 只建议在电脑无其他任务时使用。

高清成片可选 `turbo_highres_4step`，分辨率从 `1152×640` 起，且必须设为**恰好 4 步**。宽高均需是 32 的倍数、单边介于 256–1344，总像素面积不得超过 `1344×768`。本集成支持 22–362 帧、24 fps；更长故事仍建议拆成可复用的单镜头后合并。

`preview / quality / reference` 档位只属于 h3.c BF16。“新建/添加参考素材”既可连接另行准备的 vpipe Q8 Ref2VA 节点，也可连接安装模型选项 2 后的 h3.c BF16 Ref2VA 节点。完整步骤见[中文分镜教程](docs/STORYBOARD_zh-CN.md)，基础教程见 [docs/QUICKSTART_zh.md](docs/QUICKSTART_zh.md)。

### 原始 BF16 / Ref2VA 首次工作流

1. 确认已用 `./Download\ Model.command Ref2VA` 完成选项 2，且 `Doctor.command` 返回 0。
2. 从 `工作流 > 浏览模板 > ComfyUI-H3-Mac` 载入 `H3_Beginner_2_Shot_Storyboard`。
3. 在 `Load Image` 选择主体图，用“新建参考素材列表 → 添加图片/音频/媒体参考”按时间顺序连接。顺序会影响结果。
4. 两个生成节点保持 `task=Ref2VA`。首次使用模板的 `640×384 / 3s / preview / auto`；确认参考身份和动作方向后，再切换 `quality + auto`。
5. Ref2VA 的有序 references 不能同时连接首帧/尾帧锚点。如果只需要文生或首/尾帧，可只下载选项 3，手动放置 `H3 · 生成视频（Metal）`，把 `task` 改为 `FL2VA` 并清空 references。目前没有专用的 FL2VA BF16 公开模板。

48GB M5 Pro 不建议让 GPT 默认改成 `max`。`auto` 和 `low` 都会在该内存档位上以 SSD streaming 启动；`auto` 还能在持续前台压力下暂停。只有在典型工作负载下确认内存压力绿色、swap 很低，且 Mac 空闲时，才考虑常驻 `max`。

BF16 安装成功的判定标准是 `Doctor.command` 总体返回 0，并确认 FL2VA/Ref2VA、锁定模型清单、h3.c `--info` 和 `H3GenerateVideo` 节点就绪。诊断时优先收集 `runtime/comfyui-server.log` 与 `runtime/ComfyUI/output/h3-jobs/<job-id>/engine.log`，不要盲删模型缓存。

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
| H3 · 使用 vpipe Q8 多参考生成（Metal） | 调用 Q8 Ref2VA，根据有序图片、视频和音频参考保持身份并驱动表演 |
| H3 · 合并分镜 MP4 | 按顺序拼接 2–8 个已完成任务，不重跑 H3，也不重新压缩视频 |
| H3 · 添加统一固定配音 | 合并后按时间添加台词，全片始终使用同一个 macOS 音色 |

## 资源与画质档位

| 资源档位 | 调度/内存行为 | 是否改变 steps/layers/reuse |
|---|---|---|
| low | Darwin 后台调度；vpipe 使用配置的 12/8 GiB 池上限；一直推进 | 否 |
| auto | 同样保守启动；使用中/电池供电时后台慢跑，检测到压力暂时暂停，接电安静空闲 5 分钟后解除后台策略 | 否 |
| max | 正常优先级和 vpipe 默认内存策略；无自动暂停，只建议电脑空闲时使用 | 否 |

vpipe 的内存池上限在镜头启动时固定。运行中把 `auto` 切成 `max` 只能解除后台调度，不能把当前进程按另一套启动参数重建；h3.c 的常驻/SSD streaming 路径同样在启动时确定。

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
├── vpipe-status.json  # vpipe 任务
├── engine.log
├── result.partial.mp4
└── result.mp4
```

已完成的相同请求可直接复用。vpipe worker 可以跨 ComfyUI 重启继续持有任务；worker 自身退出时 launchd 会重启它，并重新接管出生指纹完全一致的存活进程组。每个 vpipe 镜头退出后默认冷却 90 秒；下一个镜头只有在公开的 macOS 内存余量、wired 占比、swap/pageout 增长连续 3 次健康时才启动。若 vpipe 仍因 wired Metal 内存不足而主动拒绝，本 worker 会保留相同提示词、参考图、种子、分辨率和帧数，冷却后自动重试一次。等待状态会写入 `vpipe-status.json`，期间不启动 Metal 进程并可取消。

**上述跨 ComfyUI 重启的 durable worker 保证只属于 vpipe 路线。** h3.c BF16 任务可在存活进程上暂停/继续，但不应在正在推理时重启 ComfyUI/launchd 服务或更新项目；引擎进程退出后，当前镜头需从头重试，已完成镜头仍可复用。

两种引擎目前都没有导出去噪步状态，因此真正退出的引擎进程仍不能从第 12/20 步精确续跑；这里的“重试”是同规格从头重跑单个失败镜头，不是从失败的去噪步续算。取消时会保留日志和残片，但未封装完成的 MP4 可能无法播放。

运行中可双击 `H3 Control.command` 查看状态、暂停、继续，或切换 `low / auto / max` 调度策略。命令行也可以直接使用：

```bash
./H3\ Control.command status
./H3\ Control.command pause
./H3\ Control.command resume
./H3\ Control.command auto
./H3\ Control.command max
```

这套控制同时适用于已注册的 h3.c 和 vpipe 任务。“暂停/继续”使用 macOS `SIGSTOP/SIGCONT`：已加载权重和进程状态仍留在内存，继续时不会从头加载或重做已完成的 CPU 侧进度。暂停不会释放统一内存，也无法撤回已经提交给 GPU 的 Metal command buffer。它不是写入磁盘的模型检查点，因此引擎进程退出或机器重启后不能从同一步继续。白天推荐 `resource=auto`，后台推进并在持续检测到前台卡顿时暂时暂停。详细说明见[资源调度与暂停](docs/RESOURCE_CONTROL_zh.md)。

服务保活与推理暂停是两套独立控制：

```bash
./Service\ Control.command status
./Service\ Control.command restart --worker-only
```

合并后的项目保存在 `output/h3-storyboards/<storyboard-id>/`。后面的镜头失败时，前面完成的单镜头任务仍可复用。

## 为什么选 ComfyUI？Manager 是什么？

ComfyUI 是可视化节点画布、执行服务、API、队列、历史记录和工作流格式。它是目前很强的本地生成式工作流开源基础，但原始节点图并不天然等于最适合纯新手的成品软件。因此本项目保留它可靠、可复用的底座，在上面增加更小、更明确的 H3 创作层。

[ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager) 是另一个扩展，负责安装、更新、启用、禁用自定义节点和模型，并保存环境快照；它不是另一套前端。本项目不依赖它，也不默认安装，因为当前一键包锁定了验证过的版本，任意更新扩展反而容易让新手环境失去可复现性。

## 量化路线

当前锁定版 h3.c 已经会在支持的 M5 上自动选择常驻 INT8 MLP/QKV/attention 投影；SSD streaming 是独立的原始 BF16 路径，并会禁用这些常驻优化。后续界面会把“调度策略”和“内存/引擎路径”拆开，而不会把量化伪装成无损加速；只有在 48GB M5 Pro 上完成相同提示词、种子、分辨率、NFE、内存和画质对照后才调整默认值。

## 验证状态

- 自动化后端测试、shell 语法和 GitHub Actions：已验证。
- 最新版锁定 ComfyUI 的 V3 节点注册：已验证。
- 官方 vpipe v0.1.37 DMG 的 SHA-256、Apple 代码签名、复制后应用包、helper 路径和固定提交身份：已在 Apple Silicon 上验证。
- Q8 目录/索引/尺寸/固定版本校验与紧凑续传流程：有自动化测试，并已对现有完整 Q8 安装校验。
- 尚未在第二台实体 M5 Pro 上从零完成一次“安装到出片”；因此项目额外提供 `Doctor.command`、首次启动日志和 worker `degraded` 明确提示，避免静默失败。

## 隐私、许可证与限制

- 生成和素材处理均在本机完成。显式选择 Neural 配音是例外：台词会发送到相应在线语音服务。
- 本仓库不包含模型、生成内容、日志或用户配置。
- 私人工作流请放进 `private_workflows/`，或命名为 `*.private.json`；两者都已 Git 忽略。
- 任何 GPT / 编码助手在 push 前都必须运行 `git status --short`，不得提交 `runtime/`、`output/`、`config.json`、素材或私人工作流。安装公开仓库不需要用户提供 GitHub token。
- 本项目代码采用 MIT License；ComfyUI、官方前端、h3.c、FFmpeg 和模型各自保留原许可证。明确的上游致谢与许可证链接见 [THIRD_PARTY.md](THIRD_PARTY.md)。
- MiniMax H3 权重受其 Community License 约束，下载前请自行阅读并确认。
- 只支持 Apple Silicon macOS。
- h3.c 要求完整原始模型目录；Ref2VA 还需要 FL2VA 基础文件。
- Ref2VA 有序参考不能与首/尾帧锚点混用。
- vpipe Q8 支持首帧 FL2VA；另行完成 Ref2VA Q8 准备后，也支持有序多参考。h3.c BF16 仍是官方原始权重路线。

## 开发

```bash
python3 -m venv .venv
.venv/bin/pip install pytest numpy pillow typing_extensions
.venv/bin/pytest -q
```

架构与扩展点见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
