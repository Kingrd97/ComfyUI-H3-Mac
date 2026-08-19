# 中文快速上手

## 1. 安装与体检

依次双击：

1. `Install.command`
2. `Doctor.command`
3. `Download Model.command`
4. 再运行一次 `Doctor.command`
5. `Start.command`

`Doctor.command` 只检查环境，不会加载大模型。

从旧版升级时，配置 schema v2 会先保存 `config.json.v1-backup`。只有完全等于旧版随附默认值的 `background` 配置会自动迁移到 `adaptive`；自己改过的行为或阈值保持不变。

启动后可在 `Comfy > Locale > Language` 选择“中文”。第一次使用建议从 `工作流 > 浏览模板 > ComfyUI-H3-Mac > H3_Beginner_2_Shot_Storyboard` 开始，不需要自己从空白画布搭节点。

## 2. 素材顺序

h3.c 不理解文件名的语义，只按接入顺序看到 `<Picture 1>`、`<Picture 2>`。建议：

1. 主体全身照
2. 主体脸部/毛色特写
3. 主要环境
4. 次要环境或动作参考

在提示词里明确写 “The cat in Picture 1 and Picture 2”，并让环境来自 Picture 3/4。不要同时连接 Ref2VA 素材和首尾帧。

## 3. 推荐提示词结构

提示词按以下顺序写，通常比堆砌形容词稳定：

```text
[主体身份和必须保持的特征]
[0-3 秒动作]
[3-7 秒动作]
[7-10 秒动作]
[环境和物理细节]
[镜头运动和景别]
[光线、真实感、声音]
[明确禁止项]
```

示例见 `examples/prompt_cat_stream.txt`。

也可以直接使用 `H3 · 编写单镜头提示词`，把上述内容分栏填写；多镜头编排和最终 MP4 合并见 [分镜创作教程](STORYBOARD_zh-CN.md)。

## 4. 两阶段创作

先用 `preview + low` 生成 2～3 秒，检查主体、场景和动作方向。满意后保持 seed 不变，改为目标时长并使用 `quality + auto`。只有当你需要判断快速参数是否损伤画质时，才用 `reference`。

## 5. 卡顿和取消

- `low` 会把 CPU/I/O 置于后台优先级，但 Metal GPU 没有公开的“限制核心数”接口。
- `auto` 默认使用自适应保护：通常在后台慢跑；无需额外权限的原生 helper 发现“最近有输入 + display-link 回调间隔/age 连续异常”时会尽快暂停，强信号不可用时再根据持续的其他 CPU、WindowServer 和 GPU 压力回退判断。framebuffer age 只用于诊断，不会单独触发暂停。系统健康稳定后先后台试跑，再自动继续。
- macOS 不能通用读取任意前台 App 的真实掉帧数；helper 观察显示系统响应，也不捕获屏幕内容。`SIGSTOP` 不能撤回已经提交的 Metal 工作，暂停也不会释放统一内存，因此目标是“尽量无感”而不是硬实时保证。
- 模型首次加载会产生明显内存和磁盘压力，加载完成后通常更平稳。
- ComfyUI 中断按钮会向 h3.c 进程组发送终止信号，日志和残片仍留在任务目录。
- 完成的同请求会复用；中间去噪步不能恢复，这是 h3.c 当前接口限制。

## 6. 常见错误

`FL2VA base model not found`：下载目录不完整或配置指向了任务子目录。`model_root` 应指向包含 `FL2VA/`、可选 `Ref2VA/` 的根目录。

`Ref2VA references cannot be combined...`：移除首帧/尾帧，或清空参考素材列表，二选一。

`h3 executable not found`：重新运行 `Install.command`，然后运行 `Doctor.command`。
