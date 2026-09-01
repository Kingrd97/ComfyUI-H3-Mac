import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

const STATE_TEXT = {
    queued: "排队中",
    launching: "正在启动",
    running: "生成中",
    paused: "已暂停",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
    unknown: "未知",
};

function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

function actionButton(label, action, job, refresh) {
    const button = element("button", "h3-job-action", label);
    button.addEventListener("click", async () => {
        button.disabled = true;
        try {
            const response = await api.fetchApi(`/h3/vpipe/jobs/${job.job_id}/${action}`, {
                method: "POST",
            });
            const data = await response.json();
            if (!response.ok || !data.ok) throw new Error(data.error || "操作失败");
            await refresh();
        } catch (error) {
            window.alert(`H3: ${error.message}`);
        } finally {
            button.disabled = false;
        }
    });
    return button;
}

app.registerExtension({
    name: "ComfyUI.H3Mac.BackgroundJobsV2",
    async setup() {
        const style = document.createElement("style");
        style.textContent = `
            #h3-jobs-button { position: fixed; right: 18px; bottom: 18px; z-index: 10020; border: 1px solid #5f63e9; border-radius: 9px; padding: 9px 13px; color: #fff; background: #3438a8; box-shadow: 0 4px 18px #0008; cursor: pointer; font-weight: 600; }
            #h3-jobs-overlay { position: fixed; inset: 0; z-index: 10030; display: none; align-items: center; justify-content: center; background: #0009; }
            #h3-jobs-panel { width: min(900px, 92vw); max-height: 86vh; overflow: hidden; display: flex; flex-direction: column; color: #eee; background: #202124; border: 1px solid #555; border-radius: 12px; box-shadow: 0 12px 45px #000c; }
            .h3-jobs-header { display: flex; align-items: center; gap: 10px; padding: 14px 16px; border-bottom: 1px solid #444; }
            .h3-jobs-header h2 { margin: 0; font-size: 18px; }
            .h3-worker-state { flex: 1; color: #aaa; }
            .h3-jobs-close, .h3-job-action { border: 1px solid #666; border-radius: 6px; padding: 6px 9px; color: #eee; background: #34363a; cursor: pointer; }
            .h3-jobs-close:hover, .h3-job-action:hover { background: #454850; }
            .h3-job-action:disabled { opacity: .45; cursor: wait; }
            #h3-jobs-list { overflow: auto; padding: 10px 14px 16px; }
            .h3-job { margin-top: 9px; padding: 11px 12px; border: 1px solid #42444a; border-radius: 9px; background: #292b2f; }
            .h3-job-top { display: flex; align-items: center; gap: 9px; }
            .h3-job-id { font: 12px ui-monospace, monospace; color: #bbb; }
            .h3-job-state { font-weight: 700; color: #85b6ff; }
            .h3-job-meta { margin: 7px 0; color: #bbb; font-size: 12px; }
            .h3-job-prompt { margin: 7px 0; color: #ddd; line-height: 1.35; }
            .h3-job-progress { height: 7px; overflow: hidden; border-radius: 5px; background: #151619; }
            .h3-job-progress > div { height: 100%; background: linear-gradient(90deg, #575ce5, #58a6ff); }
            .h3-job-message { margin-top: 6px; color: #aaa; font-size: 12px; }
            .h3-job-error { margin-top: 6px; color: #ff8f8f; font-size: 12px; white-space: pre-wrap; }
            .h3-job-controls { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; }
            .h3-job-video { color: #8fc8ff; padding: 5px; }
            .h3-jobs-empty { padding: 30px; text-align: center; color: #999; }
        `;
        document.head.appendChild(style);

        const openButton = element("button", "", "H3 后台任务");
        openButton.id = "h3-jobs-button";
        const overlay = element("div");
        overlay.id = "h3-jobs-overlay";
        const panel = element("section");
        panel.id = "h3-jobs-panel";
        const header = element("div", "h3-jobs-header");
        header.appendChild(element("h2", "", "H3 后台任务 / Background Jobs"));
        const workerState = element("div", "h3-worker-state", "正在读取…");
        header.appendChild(workerState);
        const refreshButton = element("button", "h3-jobs-close", "刷新");
        refreshButton.title = "立即刷新后台任务状态";
        header.appendChild(refreshButton);
        const closeButton = element("button", "h3-jobs-close", "关闭");
        header.appendChild(closeButton);
        const list = element("div");
        list.id = "h3-jobs-list";
        panel.append(header, list);
        overlay.appendChild(panel);
        document.body.append(openButton, overlay);

        let opened = false;
        let refreshTimer = 0;
        let refreshPromise = null;
        let disposed = false;

        const refresh = () => {
            if (refreshPromise) return refreshPromise;
            refreshPromise = (async () => {
              try {
                const response = await api.fetchApi(`/h3/vpipe/jobs?_=${Date.now()}`, {
                    cache: "no-store",
                    headers: { "Cache-Control": "no-cache" },
                });
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const data = await response.json();
                const activeJobs = data.jobs.filter((job) => ["queued", "launching", "running", "paused"].includes(job.state));
                const running = data.jobs.filter((job) => job.state === "running").length;
                openButton.textContent = activeJobs.length ? `H3 后台任务 · ${running}运行/${activeJobs.length}活动` : "H3 后台任务";
                const refreshedAt = new Date((data.snapshot_at || Date.now() / 1000) * 1000).toLocaleTimeString();
                workerState.textContent = data.worker.online
                    ? `Worker 在线 · ${data.worker.state}${data.worker.active_job ? ` · ${data.worker.active_job}` : ""}${data.worker.message ? ` · ${data.worker.message}` : ""} · 更新 ${refreshedAt}`
                    : `Worker 离线 · 更新 ${refreshedAt}`;
                if (!opened) return;
                list.replaceChildren();
                if (!data.jobs.length) {
                    list.appendChild(element("div", "h3-jobs-empty", "还没有 H3 后台任务"));
                    return;
                }
                for (const job of data.jobs) {
                    const row = element("article", "h3-job");
                    const top = element("div", "h3-job-top");
                    top.appendChild(element("span", "h3-job-state", `${STATE_TEXT[job.state] || job.state} · ${job.progress}%`));
                    top.appendChild(element("span", "h3-job-id", job.job_id));
                    row.appendChild(top);
                    const queue = job.queue_position ? ` · 队列 #${job.queue_position}` : "";
                    row.appendChild(element("div", "h3-job-meta", `Seed ${job.seed} · ${job.width}×${job.height} · ${job.frames}帧/${job.fps}fps · ${job.resource_profile}${queue}`));
                    row.appendChild(element("div", "h3-job-prompt", job.prompt_preview || "（无提示词）"));
                    const progress = element("div", "h3-job-progress");
                    const fill = element("div");
                    fill.style.width = `${job.progress}%`;
                    progress.appendChild(fill);
                    row.appendChild(progress);
                    row.appendChild(element("div", "h3-job-message", job.message || job.state));
                    if (job.error) row.appendChild(element("div", "h3-job-error", job.error));
                    const controls = element("div", "h3-job-controls");
                    if (["running", "queued", "launching"].includes(job.state)) controls.appendChild(actionButton("暂停", "pause", job, refresh));
                    if (job.state === "paused") controls.appendChild(actionButton("继续", "resume", job, refresh));
                    if (["running", "queued", "paused"].includes(job.state)) {
                        controls.appendChild(actionButton("低功耗", "low", job, refresh));
                        controls.appendChild(actionButton("自动", "auto", job, refresh));
                        controls.appendChild(actionButton("最高性能", "max", job, refresh));
                        controls.appendChild(actionButton("取消", "cancel", job, refresh));
                    }
                    if (job.state === "launching") controls.appendChild(actionButton("取消", "cancel", job, refresh));
                    if (["failed", "cancelled"].includes(job.state)) controls.appendChild(actionButton("原参数重试", "retry", job, refresh));
                    if (job.video_url) {
                        const video = element("a", "h3-job-video", "查看成品");
                        video.href = job.video_url;
                        video.target = "_blank";
                        controls.appendChild(video);
                    }
                    row.appendChild(controls);
                    list.appendChild(row);
                }
              } catch (error) {
                openButton.textContent = "H3 Worker 无法连接";
                workerState.textContent = error.message;
              }
            })().finally(() => {
                refreshPromise = null;
            });
            return refreshPromise;
        };

        const scheduleRefresh = () => {
            window.clearTimeout(refreshTimer);
            if (disposed) return;
            refreshTimer = window.setTimeout(async () => {
                await refresh();
                scheduleRefresh();
            }, 2500);
        };

        const setOpened = (value) => {
            opened = value;
            overlay.style.display = value ? "flex" : "none";
            if (value) refresh();
        };
        openButton.addEventListener("click", () => setOpened(true));
        refreshButton.addEventListener("click", refresh);
        closeButton.addEventListener("click", () => setOpened(false));
        overlay.addEventListener("click", (event) => {
            if (event.target === overlay) setOpened(false);
        });
        const refreshWhenVisible = () => {
            if (document.visibilityState === "visible") refresh();
        };
        document.addEventListener("visibilitychange", refreshWhenVisible);
        window.addEventListener("focus", refresh);
        await refresh();
        scheduleRefresh();
        window.addEventListener("beforeunload", () => {
            disposed = true;
            window.clearTimeout(refreshTimer);
            document.removeEventListener("visibilitychange", refreshWhenVisible);
            window.removeEventListener("focus", refresh);
        }, { once: true });
    },
});
