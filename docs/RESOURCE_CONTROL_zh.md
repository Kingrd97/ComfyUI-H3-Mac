# 48GB M5 Pro：资源调度、暂停与继续

推荐在 H3 生成节点中选择 `resource_profile=auto`。

## 三种模式

- `auto`：48GB M5 Pro 默认推荐。进程启动时低于 64 GiB 会使用 SSD streaming；键鼠活跃、其他进程 CPU 使用明显或使用电池时转为 macOS 后台优先级慢跑；接电且连续空闲 5 分钟后解除后台策略。两种状态都会继续推进。
- `low`：使用 SSD streaming，并让 macOS 把进程作为后台任务调度。它会一直生成，不会因为键鼠活动自动暂停。
- `max`：不自动暂停、不启用 SSD streaming。适合内存充足且明确不用电脑时；复杂 Ref2VA 常驻任务在 48GB 机器上可能非常接近统一内存上限。

自动调度每 2 秒检查一次。已经提交到 GPU 的 Metal command buffer 可能还需要短暂完成，因此人在回来操作后，暂停通常不是毫秒级立即生效。

## 控制正在运行的任务

双击项目根目录的 `H3 Control.command`，或使用：

```bash
./H3\ Control.command status   # 查看所有运行中任务
./H3\ Control.command pause    # 立即暂停
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

暂停使用 `SIGSTOP`，精确的当前计算状态和模型权重仍在统一内存中。`SIGCONT` 后从原处继续，不重新加载模型，也不重做已完成步骤。

它不能跨进程或重启：关闭 ComfyUI、终止 H3、注销、关机都会丢失这一内存状态。h3.c 目前未提供去噪中间张量的可移植磁盘检查点。项目会保存请求、日志、最近进度、失败残片和所有已经完成的镜头，避免整个分镜项目重跑。

## 自定义自动策略

编辑 `config.json`：

```json
{
  "auto_idle_seconds": 300,
  "auto_poll_seconds": 2,
  "auto_max_external_cpu_percent": 120,
  "auto_active_behavior": "background",
  "auto_require_ac_power": true
}
```

默认值 `"background"` 会让 H3 持续推进，同时由 macOS 把 CPU 和 I/O 优先让给前台。只有必须在使用电脑时让 H3 完全停下，才改成 `"pause"`。手动“暂停”始终覆盖所有资源档位。

高级用户可以把 `auto_ssd_streaming_ram_gib` 设为 `0`，让新启动的 `auto` 任务使用常驻权重，同时保留自适应调度。48GB 机器只有在代表性冒烟任务中确认内存压力保持绿色、实际前台应用同时打开且 swap 很低后才建议这样做；它不会改变已经运行的任务。
