# 48GB M5 Pro：资源调度、暂停与继续

推荐在 H3 生成节点中选择 `resource_profile=auto`。

## 三种模式

- `auto`：48GB M5 Pro 默认推荐。默认 `adaptive` 策略会在进程启动时低于 64 GiB 时使用 SSD streaming；人在使用电脑或电池供电时通常以 macOS 后台优先级慢跑，原生响应信号或持续回退压力出现时暂时暂停，恢复时先后台试跑，只有接电且安静空闲 5 分钟后才解除后台策略。
- `low`：使用 SSD streaming，并让 macOS 把进程作为后台任务调度。它会一直生成，不会因为键鼠活动自动暂停。
- `max`：不自动暂停、不启用 SSD streaming。适合内存充足且明确不用电脑时；复杂 Ref2VA 常驻任务在 48GB 机器上可能非常接近统一内存上限。

`low`、`max` 和手动“暂停”的语义保持不变；自适应暂停只作用于 `auto`。

## 自适应卡顿保护怎样判断

`Install.command` 会编译一个很小的原生 `h3-guardian` helper。它不会激活 App 或创建窗口，只读取当前会话最近输入时间和 display-link 回调时序，不捕获屏幕内容，也不需要“辅助功能”或“屏幕录制”权限。主显示器 framebuffer age 只作为诊断遥测输出，不是暂停触发条件。如果旧版 Xcode SDK 无法编译 helper，安装仍会继续，`auto` 暂时使用回退指标；升级 Command Line Tools 后重跑安装即可启用原生信号。

主要强信号是：最近发生过键盘/鼠标输入，同时 display-link 回调间隔或回调 age 在连续多个原生采样中都异常。它说明显示回调服务没有按节奏到达，调度器可以在下一次 0.5 秒控制轮询时暂停 H3，不必再等较慢的回退计时。framebuffer age 可能因正常原因显得陈旧，绝不会进入这条强触发；helper 仍不读取前台 App 自己的渲染器或 FPS。

原生强信号不可用或尚未触发时，调度器会回退到持续系统指标。默认情况下，以下任一条件持续约 2 秒才会暂停 H3：

- H3 进程组以外的进程合计 CPU 不低于 300%（按 macOS 进程统计口径，约等于 3 个 CPU 核心满载）；或
- 最近 5 秒内发生过输入，WindowServer CPU 不低于 80%，并且 GPU 利用率不低于 92%。持续的 display-link 延迟也可以和偏高的 WindowServer 或 GPU 组合判断。

控制器每 0.5 秒检查原生/用户/控制状态，每 2 秒刷新一次开销较高的进程和 GPU 回退指标。过载消失且恢复指标连续健康 15 秒后，H3 会先按后台优先级试跑 20 秒；试跑中再次过载就重新暂停，没有复发才回到正常后台慢跑。另一条独立规则是：接电、无键鼠操作满 5 分钟，且最新采样显示其他 CPU、WindowServer 和显示信号都已平稳时，`auto` 才解除 macOS 后台策略，以正常优先级全速生成。

macOS 没有公开、通用的接口可以读取任意前台 App 的真实掉帧率。因此，原生显示信号和 CPU/WindowServer/GPU 指标属于响应证据和代理信号，不能证明某个 App 确实掉了一帧。这套保护是 best-effort，不是硬实时保证。特别是 `SIGSTOP` 无法撤回已经提交给 GPU 的 Metal command buffer，暂停决策之后可能仍有一小段 GPU 工作完成；暂停也不会释放模型占用的统一内存。如果 helper 缺失或退出，`auto` 会无权限地退回指标路径，而不是要求用户开放额外权限。

## 控制正在运行的任务

双击项目根目录的 `H3 Control.command`，或使用：

```bash
./H3\ Control.command status   # 查看所有运行中任务
./H3\ Control.command pause    # 立即发出暂停信号
./H3\ Control.command resume   # 继续，仍遵循当前策略
./H3\ Control.command auto     # 自动调度并继续
./H3\ Control.command low      # 后台慢跑并继续
./H3\ Control.command max      # 强制满速并继续
```

状态保存在每个任务目录的 `process.json`，控制意图保存在 `control.json`，进度仍在 `progress.json`。

运行中切换模式只改变暂停与 macOS 调度策略。是否使用 SSD streaming 在进程启动时已经确定，不能在同一次去噪中途切换；要改变内存策略，需要以新资源档位重新启动该镜头。

所以，一个以 streaming 启动的 `auto` 任务在夜间空闲后也不会热切成权重常驻。“空闲加速”只表示解除后台调度，并不等于把 BF16 流式 block 中途变成常驻权重。

## SSD streaming 的真实代价

64 GiB 是本桥接项目的保守启发式，不是 h3.c 的硬要求。锁定版上游数据表明，SSD streaming 会把 DiT 跟踪存储从约 36.5 GiB 降到 2.0–2.1 GiB，但不同画布下单次完整 forward 会慢 26%–84%。复杂 Ref2VA 常驻示例的进程物理峰值约 40.1GB；在 48GB 机器上再叠加 macOS 和前台应用，余量会很小。

streaming 对 checkpoint 做只读、非缓存读取，不会反复改写模型，因此不能把逻辑读取量直接当成等量 SSD TBW 写入。它仍会占用磁盘带宽、功耗和散热余量。当前预设的大致模型读取量为：

| 画质档位 | 大致 checkpoint 读取量 |
|---|---:|
| preview | 144 GiB |
| balanced | 356 GiB |
| quality | 719 GiB |
| reference | 1.75 TiB |

在支持的 M5 上，常驻路径还会启用 h3.c 默认的 INT8 MLP/QKV/attention 投影，而 SSD streaming 使用原始 BF16 block。所选 steps、layers 和 reuse 不变，但两条数值路径的细节或构图可能略有差异。参见锁定版本的 [h3.c 内存与 streaming 说明](https://github.com/antirez/h3.c/tree/8974cc055ea9c02fcd14cc27dfda3e1027c05153#2-make-a-first-fast-video)。

## “暂停”和“磁盘检查点”的区别

暂停使用 `SIGSTOP`，进程状态和模型权重仍在统一内存中。`SIGCONT` 后从原处继续，不重新加载模型，也不重做已完成的 CPU 侧进度。已提交的 Metal 工作无法撤回，暂停也不会把模型占用的统一内存让给其他应用。

它不能跨进程或重启：关闭 ComfyUI、终止 H3、注销、关机都会丢失这一内存状态。h3.c 目前未提供去噪中间张量的可移植磁盘检查点。项目会保存请求、日志、最近进度、失败残片和所有已经完成的镜头，避免整个分镜项目重跑。

## 自定义自动策略

编辑 `config.json`：

```json
{
  "auto_idle_seconds": 300,
  "auto_poll_seconds": 0.5,
  "auto_metrics_poll_seconds": 2,
  "auto_max_external_cpu_percent": 120,
  "auto_active_behavior": "adaptive",
  "auto_jank_interaction_seconds": 5,
  "auto_jank_pause_seconds": 2,
  "auto_jank_recover_seconds": 15,
  "auto_jank_probe_seconds": 20,
  "auto_jank_cpu_percent": 300,
  "auto_jank_window_server_percent": 80,
  "auto_jank_window_server_recover_percent": 50,
  "auto_jank_gpu_percent": 92,
  "auto_jank_gpu_recover_percent": 70,
  "auto_require_ac_power": true
}
```

默认值 `"adaptive"` 就是上面介绍的策略。把 `auto_active_behavior` 设成 `"background"` 可关闭自动响应暂停，让电脑使用中始终按后台优先级推进；设成 `"pause"` 则保留旧的严格策略，只要有人操作电脑就停止 H3。手动“暂停”始终覆盖所有资源档位。`auto_max_external_cpu_percent` 仍用于判断 5 分钟空闲加速时 CPU 是否足够低；`auto_jank_*` 用来调整持续指标回退和恢复状态机。原生 display-link 回调信号采用内部保守时序，并不宣称测到了另一个 App 的 FPS；framebuffer age 始终只用于诊断。

已有安装升级到配置 schema v2 时，会先把原文件备份为 `config.json.v1-backup`。只有各项都完全匹配旧版随附默认值的 `background` 配置才会改成 `adaptive`；任何自定义行为或阈值都会保留，因此升级不会悄悄覆盖用户有意设置的资源策略。

高级用户可以把 `auto_ssd_streaming_ram_gib` 设为 `0`，让新启动的 `auto` 任务使用常驻权重，同时保留自适应调度。48GB 机器只有在代表性冒烟任务中确认内存压力保持绿色、实际前台应用同时打开且 swap 很低后才建议这样做；它不会改变已经运行的任务。
