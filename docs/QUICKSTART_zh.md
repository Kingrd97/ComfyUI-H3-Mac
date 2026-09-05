# 中文快速上手

## 1. 安装与体检

依次双击：

1. `Install.command`
2. `Download Model.command`：速度/空间优先选 `1) vpipe Q8 FL2VA`；48GB M5 Pro 需要官方原始权重和多参考时选 `2) h3.c Ref2VA BF16`
3. `Doctor.command`
4. `Start.command`

`Doctor.command` 不会运行生成，也不会把整套 BF16 权重加载到统一内存。如果已安装 Q8，它会顺序读取约 67.5 GiB 做完整 SHA-256 校验；BF16 路线会校验锁定清单、文件尺寸/内容寻址链接，再调用 h3.c `--info`。

从旧版升级时，配置 schema v4 会先保存备份；自己改过的行为、阈值与有效外部工作目录保持不变，旧的未锁定 vpipe 命令会迁到项目内已验证的二进制。

vpipe Q8 最终约 67.5 GiB（模型加两套 LoRA），首次紧凑准备建议留出至少 120 GiB。h3.c Ref2VA BF16 会同时包含 FL2VA 基础，实际约 196 GiB，建议下载前至少有 220 GiB 可用。两条路线都可 Ctrl-C 中断并通过重跑相同命令复用缓存；48GB M5 Pro 推荐 `auto`。

启动后可在 `Comfy > Locale > Language` 选择“中文”。完成推荐的 vpipe Q8 模型准备后，第一次使用建议从 `工作流 > 浏览模板 > ComfyUI-H3-Mac > H3_vpipe_Q8_2_Shot_Fixed_Voice` 开始，不需要自己从空白画布搭节点。`H3_Beginner_2_Shot_Storyboard` 属于需要额外 BF16/Ref2VA 权重的高级模板。

如果只安装原始 BF16，命令行可直接使用 `./Download\ Model.command Ref2VA`（用户本人阅读许可证并输入 `AGREE`），不需要准备 Q8。载入 `H3_Beginner_2_Shot_Storyboard`，保持 `Ref2VA / preview / auto`。BF16-only 时 vpipe worker 显示等待 Q8 资产属于预期状态，不影响 `H3 · 生成视频（Metal）`。

## 2. 先用推荐的 vpipe Q8 模板

载入 `H3_vpipe_Q8_2_Shot_Fixed_Voice`后，每个镜头只需一张首帧图：

1. 在 `Load Image` 选该镜头的首帧。
2. 在“编写单镜头提示词”填主体、分段动作、环境和镜头。
3. 在“使用 vpipe Q8 生成”先保留 `960×544 / 124 帧 / 6 步 / turbo_544p / auto`。
4. 所有镜头生成后再合并 MP4；最后才加一次统一旁白。

默认 vpipe Q8 模板是 FL2VA 首帧路线。需要 Q8 多图/视频/音频参考时，先完成 FL2VA，再运行 `./Prepare\ vpipe\ Ref2VA\ Q8.command low`，将“新建/添加参考素材”连接到“使用 vpipe Q8 多参考生成”节点；音频不能作为唯一参考。原始权重用户仍可选择 h3.c BF16/Ref2VA 路线。

## 3. 推荐提示词结构

每个 vpipe 单镜头的提示词按以下顺序写，通常比堆砌形容词稳定：

```text
[主体身份和必须保持的特征]
[0–1.5 秒动作]
[1.5–3.5 秒动作]
[3.5–5 秒动作]
[环境和物理细节]
[镜头运动和景别]
[光线、真实感、声音]
[明确禁止项]
```

示例见 `examples/prompt_cat_stream.txt`。

也可以直接使用 `H3 · 编写单镜头提示词`，把上述内容分栏填写；多镜头编排和最终 MP4 合并见 [分镜创作教程](STORYBOARD_zh-CN.md)。

## 4. 两阶段创作

先在 `turbo_544p` 保留 6 步，用较短帧数检查主体、场景和动作方向；满意后保持 seed 和提示词，再调整帧数。要做高清正式版时选 `turbo_highres_4step`，从 `1152×640` 起并设为恰好 4 步。所有 vpipe 视频固定为 24 fps，长故事用多镜头合并。

`preview / quality / reference` 是 h3.c BF16 节点的档位，不适用于 vpipe Q8 节点。

## 5. 卡顿和取消

- `low` 会把 CPU/I/O 置于后台优先级，但 Metal GPU 没有公开的“限制核心数”接口。
- `auto` 默认使用自适应保护：通常在后台慢跑；无需额外权限的原生 helper 发现“最近有输入 + display-link 回调间隔/age 连续异常”时会尽快暂停，强信号不可用时再根据持续的其他 CPU、WindowServer 和 GPU 压力回退判断。framebuffer age 只用于诊断，不会单独触发暂停。系统健康稳定后先后台试跑，再自动继续。
- macOS 不能通用读取任意前台 App 的真实掉帧数；helper 观察显示系统响应，也不捕获屏幕内容。`SIGSTOP` 不能撤回已经提交的 Metal 工作，暂停也不会释放统一内存，因此目标是“尽量无感”而不是硬实时保证。
- 模型首次加载会产生明显内存和磁盘压力，加载完成后通常更平稳。
- ComfyUI 中的“H3 后台任务”面板可暂停、继续或取消 launchd worker 接管的 vpipe 任务；日志和残片仍留在任务目录。
- 完成的同请求会复用；中间去噪步不能恢复，这是 h3.c 当前接口限制。

## 6. 常见错误

`FL2VA base model not found`：下载目录不完整或配置指向了任务子目录。`model_root` 应指向包含 `FL2VA/`、可选 `Ref2VA/` 的根目录。

`Ref2VA references cannot be combined...`：移除首帧/尾帧，或清空参考素材列表，二选一。

`h3 executable not found`：重新运行 `Install.command`，然后运行 `Doctor.command`。
