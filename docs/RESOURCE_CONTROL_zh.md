# 48GB M5 Pro：资源调度、暂停与继续

推荐在 H3 生成节点中选择 `resource_profile=auto`。

## 三种模式

- `auto`：48GB M5 Pro 默认推荐。低于 64 GiB 时使用 SSD streaming；键鼠活跃、其他进程 CPU 使用明显或使用电池时暂停；接电且连续空闲 60 秒后恢复并解除后台策略。
- `low`：使用 SSD streaming，并让 macOS 把进程作为后台任务调度。它会一直生成，不会因为键鼠活动自动暂停。
- `max`：不自动暂停、不启用 SSD streaming。适合内存充足且明确不用电脑时；48GB 运行原始 BF16 Ref2VA 可能非常接近统一内存上限。

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

## “暂停”和“磁盘检查点”的区别

暂停使用 `SIGSTOP`，精确的当前计算状态和模型权重仍在统一内存中。`SIGCONT` 后从原处继续，不重新加载模型，也不重做已完成步骤。

它不能跨进程或重启：关闭 ComfyUI、终止 H3、注销、关机都会丢失这一内存状态。h3.c 目前未提供去噪中间张量的可移植磁盘检查点。项目会保存请求、日志、最近进度、失败残片和所有已经完成的镜头，避免整个分镜项目重跑。

## 自定义自动策略

编辑 `config.json`：

```json
{
  "auto_idle_seconds": 60,
  "auto_poll_seconds": 2,
  "auto_max_external_cpu_percent": 120,
  "auto_active_behavior": "pause",
  "auto_require_ac_power": true
}
```

如果希望人在使用电脑时 H3 仍低速推进，把 `auto_active_behavior` 改成 `"background"`。若最看重前台流畅度，保留默认的 `"pause"`。
