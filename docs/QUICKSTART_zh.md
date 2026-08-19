# 中文快速上手

## 1. 安装与体检

依次双击：

1. `Install.command`
2. `Doctor.command`
3. `Download Model.command`
4. 再运行一次 `Doctor.command`
5. `Start.command`

`Doctor.command` 只检查环境，不会加载大模型。

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

## 4. 两阶段创作

先用 `preview + low` 生成 2～3 秒，检查主体、场景和动作方向。满意后保持 seed 不变，改为目标时长并使用 `quality + auto`。只有当你需要判断快速参数是否损伤画质时，才用 `reference`。

## 5. 卡顿和取消

- `low` 会把 CPU/I/O 置于后台优先级，但 Metal GPU 没有公开的“限制核心数”接口。
- 模型首次加载会产生明显内存和磁盘压力，加载完成后通常更平稳。
- ComfyUI 中断按钮会向 h3.c 进程组发送终止信号，日志和残片仍留在任务目录。
- 完成的同请求会复用；中间去噪步不能恢复，这是 h3.c 当前接口限制。

## 6. 常见错误

`FL2VA base model not found`：下载目录不完整或配置指向了任务子目录。`model_root` 应指向包含 `FL2VA/`、可选 `Ref2VA/` 的根目录。

`Ref2VA references cannot be combined...`：移除首帧/尾帧，或清空参考素材列表，二选一。

`h3 executable not found`：重新运行 `Install.command`，然后运行 `Doctor.command`。
