# 48GB M5 Pro：资源调度、暂停与继续

推荐在 H3 生成节点中选择 `resource_profile=auto`。

## 三种模式

- `auto`：48GB M5 Pro 默认推荐。默认 `adaptive` 策略会在进程启动时低于 64 GiB 时使用 SSD streaming；人在使用电脑或电池供电时通常以 macOS 后台优先级慢跑，原生响应信号或持续回退压力出现时暂时暂停，恢复时先后台试跑，只有接电且安静空闲 5 分钟后才解除后台策略。
- `low`：使用 SSD streaming，并让 macOS 把进程作为后台任务调度。它会一直生成，不会因为键鼠活动自动暂停。
- `max`：不自动暂停、不启用 SSD streaming。适合内存充足且明确不用电脑时；复杂 Ref2VA 常驻任务在 48GB 机器上可能非常接近统一内存上限。

`low`、`max` 和手动“暂停”的语义保持不变；自适应暂停只作用于 `auto`。

低于 64 GiB 的 Mac，生成节点普通上限为单镜头 5 秒；更长内容请用分镜合并。h3.c 的硬上限仍是 362 帧（约 15.08 秒），但 `H3_ALLOW_LARGE_JOB=1` 只作为专家显式开关：[h3.c issue #5](https://github.com/antirez/h3.c/issues/5) 报告了 64GB M4 Max 在 10 秒、960×544 的 VAE 解码阶段把 swap 推到约 64 GiB。打开开关并不代表 48GB 机器一定能安全完成。

## 自适应卡顿保护怎样判断

`Install.command` 会编译一个很小的原生 `h3-guardian` helper。它只在调度策略为 `auto` 时运行，意外退出后会自动重启，切换到 `low` 或 `max` 时会停止。它不会激活 App 或创建窗口，只读取当前会话最近输入时间和 display-link 回调时序，同时上报 macOS 温度状态和低电量模式；不捕获屏幕内容，也不需要“辅助功能”或“屏幕录制”权限。主显示器 framebuffer age 只作为诊断遥测输出，不是暂停触发条件；睡眠唤醒或显示器重配会清空短时节奏窗口，避免误判成前台卡顿。如果旧版 Xcode SDK 无法编译 helper，安装仍会继续，`auto` 暂时使用回退指标；升级 Command Line Tools 后重跑安装即可启用原生信号。

主要强信号是：最近发生过键盘/鼠标输入，同时 display-link 回调间隔或回调 age 在连续多个原生采样中都异常。它说明显示回调服务没有按节奏到达，调度器可以在下一次 0.5 秒控制轮询时暂停 H3，不必再等较慢的回退计时。framebuffer age 可能因正常原因显得陈旧，绝不会进入这条强触发；helper 仍不读取前台 App 自己的渲染器或 FPS。

原生强信号不可用或尚未触发时，调度器会回退到持续系统指标。默认情况下，以下任一条件持续约 2 秒才会暂停 H3：

- H3 进程组以外的进程合计 CPU 不低于 300%（按 macOS 进程统计口径，约等于 3 个 CPU 核心满载）；或
- 最近 5 秒内发生过输入，WindowServer CPU 不低于 80%，并且 GPU 利用率不低于 92%。持续的 display-link 延迟也可以和偏高的 WindowServer 或 GPU 组合判断。

控制器每 0.5 秒检查原生/用户/控制状态，每 2 秒刷新一次开销较高的进程和 GPU 回退指标。过载消失且恢复指标连续健康 15 秒后，H3 会先按后台优先级试跑 20 秒；试跑中再次过载就重新暂停，没有复发才回到正常后台慢跑。另一条独立规则是：接电、无键鼠操作满 5 分钟，且最新采样显示其他 CPU、WindowServer 和显示信号都已平稳时，`auto` 才解除 macOS 后台策略，以正常优先级全速生成。

`auto` 还会每 10 秒读取一次系统公开的 `memory_pressure`、`vm.swapusage` 和 `vm_stat` 诊断。建议可用内存比例降到 8% 或更低、温度达到 serious/critical，或 swap/pageout 增长速率越过配置阈值时，会立即暂停；恢复仍需经过健康等待和后台试跑，内存比例至少回到 15%。温度为 fair、开启低电量模式、内存尚未恢复、使用电池或前台仍繁忙时，都不会进入空闲满速。它们是保守代理指标；暂停只能阻止压力继续增加，不能驱逐 H3 已占用的统一内存。

macOS `taskpolicy` 同样是 best-effort。后台/前台策略设置失败时不会假装成功，而会在退避后重试；它只影响 CPU/I/O 调度优先级，不是 Metal GPU 硬配额。实时状态只在状态改变或每 15 秒写一次，不再跟随每次指标采样落盘。

macOS 没有公开、通用的接口可以读取任意前台 App 的真实掉帧率。因此，原生显示信号和 CPU/WindowServer/GPU 指标属于响应证据和代理信号，不能证明某个 App 确实掉了一帧。这套保护是 best-effort，不是硬实时保证。特别是 `SIGSTOP` 无法撤回已经提交给 GPU 的 Metal command buffer，暂停决策之后可能仍有一小段 GPU 工作完成；暂停也不会释放模型占用的统一内存。如果 helper 缺失或退出，`auto` 会无权限地退回指标路径，而不是要求用户开放额外权限。

引擎使用 `caffeinate -s` 包装：只有接电时才阻止系统因空闲睡眠，电池供电时仍遵循 macOS 正常睡眠策略。vpipe 由 launchd 保活的 worker 持有，而不是 ComfyUI 的一次性子进程，因此重启界面不会杀死推理；worker 重启时只会接管出生指纹完全一致的进程组。ComfyUI 启动时不再自动终止孤儿任务；确实需要清理时，使用 `H3 Control.command` 中显式的“清理已确认孤儿进程”。这是进程保活，不是可落盘的去噪检查点。

vpipe 队列还带独立的启动前内存闸门。默认在上一镜头的引擎退出后等待 90 秒，然后每 5 秒读取 `memory_pressure`、`vm.swapusage` 和 `vm_stat`；建议可回收余量至少 6144 MiB、建议空闲比例至少 20%、system wired 不高于物理内存的 18%，并且 swap/pageout 没有越过自适应调度的增长阈值。连续 3 次健康后才启动下一镜头。如果引擎日志明确包含 vpipe 的 Metal 内存拒绝标记，会再次冷却并按同规格自动重试一次。所有阈值都可以通过 `config.json` 中的 `vpipe_worker_*` 项覆盖。

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

已注册的 h3.c 与 vpipe 任务都把状态保存在 `process.json`、控制意图保存在 `control.json`；引擎/界面进度保存在 `progress.json` 或 `vpipe-status.json`。

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
  "auto_health_poll_seconds": 10,
  "auto_status_interval_seconds": 15,
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
  "auto_memory_pause_percent": 8,
  "auto_memory_recover_percent": 15,
  "auto_swap_growth_pause_mib_per_minute": 512,
  "auto_pageout_pause_mib_per_minute": 256,
  "auto_require_ac_power": true,
  "vpipe_worker_cooldown_seconds": 90,
  "vpipe_worker_memory_poll_seconds": 5,
  "vpipe_worker_memory_stable_samples": 3,
  "vpipe_worker_min_memory_free_percent": 20,
  "vpipe_worker_min_reclaimable_mb": 6144,
  "vpipe_worker_max_wired_percent": 18,
  "vpipe_worker_memory_retry_limit": 1
}
```

默认值 `"adaptive"` 就是上面介绍的策略。把 `auto_active_behavior` 设成 `"background"` 可关闭自动响应暂停，让电脑使用中始终按后台优先级推进；设成 `"pause"` 则保留旧的严格策略，只要有人操作电脑就停止 H3。手动“暂停”始终覆盖所有资源档位。`auto_max_external_cpu_percent` 仍用于判断 5 分钟空闲加速时 CPU 是否足够低；`auto_jank_*` 用来调整持续指标回退和恢复状态机。原生 display-link 回调信号采用内部保守时序，并不宣称测到了另一个 App 的 FPS；framebuffer age 始终只用于诊断。

已有安装升级到配置 schema v2 时，会先把原文件备份为 `config.json.v1-backup`。只有各项都完全匹配旧版随附默认值的 `background` 配置才会改成 `adaptive`；任何自定义行为或阈值都会保留，因此升级不会悄悄覆盖用户有意设置的资源策略。

高级用户可以把 `auto_ssd_streaming_ram_gib` 设为 `0`，让新启动的 `auto` 任务使用常驻权重，同时保留自适应调度。48GB 机器只有在代表性冒烟任务中确认内存压力保持绿色、实际前台应用同时打开且 swap 很低后才建议这样做；它不会改变已经运行的任务。
