import { lumiviaImageAccount, lumiviaJobHeaders } from "@/components/layout/lumivia-account";
import { canvasRequest, privateRequest, type CloudFile } from "./cloud-storage";

export type JobKind = "image" | "video";
export const jobLabels = { queued: "排队中", submitting: "正在提交", waiting: "正在生成", completed: "已完成", failed: "未完成", review: "待核对", cancelled: "已取消", staging: "上传中" };
export type JobStatus = keyof typeof jobLabels;
export type JobRow = { id: string; kind: JobKind; model: string; status: JobStatus; message: string; created: number; updated: number; external: string | null };
export type CanvasJob = JobRow & { label: string; assets: CloudFile[] };
export const jobPending = (status: JobStatus) => ["queued", "submitting", "waiting", "staging"].includes(status);
export const jobColor = (status: JobStatus) => status === "completed" ? "green" : status === "review" ? "gold" : status === "failed" ? "red" : jobPending(status) ? "blue" : "default";

export const canvasJobs = {
    async create(kind: JobKind, spec: { model: string } & Record<string, unknown>, reference?: File | File[], callerSignal?: AbortSignal, account = lumiviaImageAccount()): Promise<CanvasJob> {
        const timeout = AbortSignal.timeout(130_000);
        const signal = callerSignal ? AbortSignal.any([callerSignal, timeout]) : timeout;
        const grant = await lumiviaJobHeaders(kind, spec.model, account, signal);
        const files = reference instanceof File ? [reference] : reference || [];
        const data = files.length ? new FormData() : spec;
        if (data instanceof FormData) {
            data.set("spec", JSON.stringify(spec));
            files.forEach((file) => data.append("reference", file));
        }
        return (await canvasRequest(`jobs/${kind}`, grant, data, signal, account)).json();
    },
    async get(id: string, signal: AbortSignal, account = lumiviaImageAccount()): Promise<CanvasJob> {
        return (await privateRequest("jobs/get", { id }, signal, account)).json();
    },
    async list(page: number, signal: AbortSignal): Promise<{ items: CanvasJob[]; more: boolean }> {
        return (await privateRequest("jobs/list", { page }, signal)).json();
    },
    async cancel(id: string, signal: AbortSignal) {
        await privateRequest("jobs/cancel", { id }, signal);
    },
    async overview(page: number, signal: AbortSignal): Promise<{ items: (JobRow & { owner: number })[]; counts: Partial<Record<JobStatus, number>>; more: boolean; activeWorkers: number; memoryBytes: number | null; memoryPaused: boolean }> {
        return (await privateRequest("jobs/admin-jobs", { page }, signal)).json();
    },
};

export async function waitForCanvasJob(initial: CanvasJob, signal: AbortSignal, account: ReturnType<typeof lumiviaImageAccount>) {
    let task = initial;
    while (jobPending(task.status)) {
        await new Promise<void>((resolve, reject) => {
            signal.throwIfAborted();
            const timer = window.setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, 5000);
            function abort() { window.clearTimeout(timer); reject(signal.reason); }
            signal.addEventListener("abort", abort, { once: true });
        });
        try {
            task = await canvasJobs.get(task.id, AbortSignal.any([signal, AbortSignal.timeout(25_000)]), account);
        } catch (error) {
            if (signal.aborted) throw error;
            throw new Error(`${(error as Error).message}。请到生成记录查看任务 ${task.id.slice(0, 10)}`);
        }
    }
    return task;
}
