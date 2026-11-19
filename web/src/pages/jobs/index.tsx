import { useEffect, useRef, useState } from "react";
import { App, Button, Empty, Form, Input, InputNumber, Select, Spin, Tag } from "antd";
import { ImagePlus, RefreshCw, Video, X } from "lucide-react";
import { Link, useLocation } from "react-router-dom";
import { CloudFileActions } from "@/components/cloud-file-actions";
import { useLumiviaAccount, type Account } from "@/components/layout/lumivia-account";
import { canvasJobs, jobColor, jobLabels, jobPending, type CanvasJob, type JobKind } from "@/services/api/canvas-jobs";

const date = (value: number) => new Date(value).toLocaleString();
const opts = (values: string[]) => values.map((value) => ({ label: value, value }));
const fileAccept = "image/png,image/jpeg,image/webp";

export default function JobsPage() {
    const { pathname } = useLocation();
    const account = useLumiviaAccount((s) => s.account);
    const syncing = useLumiviaAccount((s) => s.syncing);
    const kind = pathname === "/video" ? "video" : "image";
    return <div className="h-full overflow-y-auto">
        <div className="mx-auto max-w-6xl px-4 py-7 sm:px-8 sm:py-10">
            <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
                <div><div className="mb-1 text-xs text-muted-foreground">乐米画布 / 工作台</div><h1 className="text-2xl font-semibold sm:text-3xl">生成中心</h1>
                    <p className="mt-2 text-sm text-muted-foreground">任务在服务端运行。离开页面后，仍可在这里找回进度与结果。</p></div>
                <div className="flex flex-wrap items-center gap-2"><Link className="rounded-md border border-border px-3 py-1.5 text-sm" to="/image">生图工作台</Link><Link className="rounded-md border border-border px-3 py-1.5 text-sm" to="/storage">云图库</Link></div>
            </div>
            <div className="mb-5 flex gap-2" aria-label="生成类型">
                {(["image", "video"] as const).map((value) => <Link key={value} to={value === "image" ? "/jobs" : "/video"} aria-current={kind === value ? "page" : undefined}
                    className={`flex items-center gap-2 rounded-lg border px-4 py-2 text-sm ${kind === value ? "border-foreground bg-foreground text-background" : "border-border text-muted-foreground"}`}>
                    {value === "image" ? <ImagePlus size={16} /> : <Video size={16} />}{value === "image" ? "后台生图" : "视频生成"}</Link>)}
            </div>
            {account ? <Workspace key={account.id} account={account} kind={kind} /> : syncing ? <Spin /> : <Empty description="请先登录乐米主站，再打开画布" />}
        </div>
    </div>;
}

function Workspace({ account, kind }: { account: Account; kind: JobKind }) {
    const { message } = App.useApp();
    const [page, setPage] = useState(1);
    const [version, refresh] = useState(0);
    const [list, setList] = useState<{ items: CanvasJob[]; more: boolean }>({ items: [], more: false });
    const [loading, setLoading] = useState(true);
    const [problem, setProblem] = useState("");
    useEffect(() => {
        const controller = new AbortController();
        let next: number | undefined;
        async function load() {
            if (document.hidden) {
                if (!controller.signal.aborted) next = window.setTimeout(() => void load(), 8000);
                return;
            }
            try {
                const current = await canvasJobs.list(page, controller.signal);
                if (controller.signal.aborted) return;
                setList(current);
                setProblem("");
                if (current.items.some((item) => jobPending(item.status))) next = window.setTimeout(() => void load(), 6500);
            } catch (error) {
                if (!controller.signal.aborted) setProblem((error as Error).message);
            } finally {
                if (!controller.signal.aborted) setLoading(false);
            }
        }
        setLoading(true);
        void load();
        return () => { controller.abort(); window.clearTimeout(next); };
    }, [page, version]);
    const reload = () => refresh((v) => v + 1);
    return <div className="grid items-start gap-5 lg:grid-cols-[minmax(18rem,0.85fr)_minmax(24rem,1.15fr)]">
        <JobForm key={kind} kind={kind} account={account} onQueued={() => { setPage(1); reload(); }} />
        <section className="min-w-0 rounded-xl border border-border bg-card p-4 sm:p-5">
            <div className="mb-3 flex items-center justify-between gap-2"><div><h2 className="font-semibold">我的生成记录</h2><p className="mt-1 text-xs text-muted-foreground">仅当前账号可见。打开原文件时才读取原图或视频。</p></div>
                <Button size="small" loading={loading} icon={<RefreshCw size={14} />} onClick={reload}>刷新</Button></div>
            {problem && <p role="alert" className="mb-3 text-sm text-red-500">{problem}</p>}
            {!list.items.length && (loading ? <Spin /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有后台生成记录" />)}
            <div className="divide-y divide-border">
                {list.items.map((task) => <article key={task.id} className="space-y-2 py-3">
                    <div className="flex flex-wrap items-center gap-2"><Tag color={jobColor(task.status)}>{jobLabels[task.status]}</Tag><span className="text-xs text-muted-foreground">{task.kind === "image" ? "图片" : "视频"} · {date(task.created)}</span>
                        {task.status === "queued" && <Button type="link" size="small" className="ml-auto" onClick={async () => { try { await canvasJobs.cancel(task.id, AbortSignal.timeout(20_000)); reload(); } catch (error) { message.error((error as Error).message); reload(); } }}>取消排队</Button>}</div>
                    <p className="line-clamp-2 break-words text-sm font-medium" title={task.label}>{task.label}</p>
                    <p className="break-all text-xs text-muted-foreground">{task.model} · {task.id.slice(0, 10)}</p>
                    {task.message && <p className="text-xs text-muted-foreground">{task.message}</p>}
                    {task.assets.length > 0 && <ul className="space-y-1">{task.assets.map((file, index) => <li key={file.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md bg-muted/50 p-2">
                        <span className="flex items-center gap-2 text-xs">{file.thumbUrl ? <img src={file.thumbUrl} alt="" width={44} height={44} className="h-11 w-11 rounded object-cover" loading="lazy" decoding="async" /> : <Video size={18} />}
                            结果 {index + 1}{file.expires ? ` · ${date(file.expires)} 到期` : ""}</span><CloudFileActions file={file} />
                    </li>)}</ul>}
                    {task.status === "completed" && !task.assets.length && <p className="text-xs text-muted-foreground">文件已过期或已从云图库删除。</p>}
                </article>)}
            </div>
            <div className="mt-4 flex items-center gap-3 text-xs"><Button size="small" disabled={page === 1 || loading} onClick={() => setPage((p) => p - 1)}>上一页</Button>第 {page} 页<Button size="small" disabled={!list.more || loading} onClick={() => setPage((p) => p + 1)}>下一页</Button></div>
        </section>
    </div>;
}

function JobForm({ account, kind, onQueued }: { account: Account; kind: JobKind; onQueued: () => void }) {
    const { message } = App.useApp();
    const [form] = Form.useForm();
    const [reference, setReference] = useState<File>();
    const input = useRef<HTMLInputElement>(null);
    const [sending, setSending] = useState(false);
    const busy = useRef(false);
    const attempted = useRef<{ signature: string; file?: File; id: string }>(null);
    const models = kind === "image" ? account.models : account.videoModels || [];
    const model = Form.useWatch("model", form);
    const videoOptions = account.videoOptions?.[model || models[0]];
    const videoDefault = account.videoOptions?.[models[0]];
    const defaultResolution = videoDefault?.resolutions.includes("720p") ? "720p" : videoDefault?.resolutions[0] ?? "720p";
    const durations = videoOptions?.durations || [];
    const resolutions = videoOptions?.resolutions || ["480p", "720p", "1080p"];
    const size = Form.useWatch("size", form);
    const count = Form.useWatch("count", form) ?? 1;
    const price = size === "1024x1024" ? account.prices["1k"] : size === "2048x2048" ? account.prices["2k"] : size === "3840x2160" ? account.prices["4k"] : undefined;
    async function submit(values: { model: string; prompt: string } & Record<string, unknown>) {
        if (busy.current || !models.includes(values.model)) return;
        busy.current = true;
        setSending(true);
        const signature = JSON.stringify(values);
        if (attempted.current?.signature !== signature || attempted.current.file !== reference) {
            attempted.current = { signature, file: reference, id: crypto.randomUUID().replaceAll("-", "") };
        }
        try {
            await canvasJobs.create(kind, { ...values, id: attempted.current.id }, reference);
            attempted.current = null;
            message.success("已接收任务，可在生成记录中查看");
            form.setFieldValue("prompt", "");
            setReference(undefined);
            onQueued();
        } catch (error) {
            message.error((error as Error).message);
            onQueued();
        } finally { busy.current = false; setSending(false); }
    }
    return <section className="rounded-xl border border-border bg-card p-4 sm:p-5">
        <h2 className="mb-1 font-semibold">{kind === "image" ? "提交后台生图" : "提交视频任务"}</h2>
        <p className="mb-4 text-xs text-muted-foreground">{kind === "image" ? account.group : account.videoGroups?.[model || models[0]] || "视频渠道"} · 费用按当前账号的主站分组结算。</p>
        {!models.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={kind === "video" ? "视频功能未开放，或账号没有视频分组权限" : "当前账号没有可用的图片模型"}>
            <Button onClick={() => void useLumiviaAccount.getState().sync()}>重新同步账号</Button>{kind === "video" && <Link to="/video/local" className="ml-3 text-sm underline">本机视频历史</Link>}
        </Empty> : <Form form={form} layout="vertical" initialValues={{ model: models[0], size: "1024x1024", quality: "auto", count: 1, seconds: videoDefault?.durations[0] ?? Math.min(6, account.videoSeconds || 12), ratio: "16:9", resolution: defaultResolution }} onFinish={submit} disabled={sending}>
            <Form.Item name="model" label="模型" rules={[{ required: true }]}><Select options={opts(models)} showSearch onChange={(value: string) => {
                if (kind !== "video") return;
                const selected = account.videoOptions?.[value];
                const currentResolution = form.getFieldValue("resolution");
                const currentSeconds = form.getFieldValue("seconds");
                if (selected?.resolutions.length && !selected.resolutions.includes(currentResolution)) form.setFieldValue("resolution", selected.resolutions[0]);
                if (selected?.durations.length && !selected.durations.includes(currentSeconds)) form.setFieldValue("seconds", selected.durations[0]);
            }} /></Form.Item>
            <Form.Item name="prompt" label="提示词" rules={[{ required: true, whitespace: true, max: 4000, message: "请填写提示词，最多 4000 字" }]}><Input.TextArea rows={5} maxLength={4000} showCount placeholder={kind === "image" ? "描述画面、光线和风格" : "描述场景、主体动作和镜头"} /></Form.Item>
            <div className="grid grid-cols-2 gap-x-3">
                {kind === "image" ? <>
                    <Form.Item name="size" label="尺寸"><Select options={opts(["1024x1024", "1536x1024", "1024x1536", "2048x2048", "3840x2160", "auto"])} /></Form.Item>
                    <Form.Item name="quality" label="画质"><Select options={opts(["auto", "low", "medium", "high"])} /></Form.Item>
                    <Form.Item name="count" label="张数" rules={[{ required: true }]}><InputNumber min={1} max={4} precision={0} className="!w-full" /></Form.Item>
                </> : <>
                    <Form.Item name="resolution" label="分辨率" rules={[{ required: true }]}><Select options={opts(resolutions)} /></Form.Item>
                    <Form.Item name="ratio" label="画面比例"><Select options={opts(["16:9", "9:16", "1:1"])} /></Form.Item>
                    <Form.Item name="seconds" label="时长（秒）" rules={[{ required: true }]}>{durations.length ? <Select options={durations.map((value) => ({ value }))} /> : <InputNumber min={4} max={account.videoSeconds || 12} precision={0} className="!w-full" />}</Form.Item>
                </>}
            </div>
            <div className="mb-4 flex flex-wrap items-center gap-2 text-xs text-muted-foreground"><input type="file" accept={fileAccept} hidden ref={input} onChange={(e) => { setReference(e.currentTarget.files?.[0]); e.currentTarget.value = ""; }} />
                <Button size="small" onClick={() => input.current?.click()}>{kind === "image" ? "添加参考图" : "添加首帧"}</Button>
                {reference ? <><span className="max-w-40 truncate" title={reference.name}>{reference.name}</span><Button size="small" type="text" icon={<X size={14} />} aria-label="移除参考图" onClick={() => setReference(undefined)} /></> : <span>可选，1 张 PNG、JPG 或 WebP</span>}
            </div>
            <div className="mb-3 text-xs leading-5 text-muted-foreground">{kind === "image" && typeof price === "number" ? `该尺寸参考费用 ¥${(price * count).toFixed(4).replace(/0+$/, "").replace(/\.$/, "")}，以主站账单为准。` : "费用以主站用量记录为准。"} 结果自动存入云图库，并占用存储额度。</div>
            <Button type="primary" htmlType="submit" block loading={sending} disabled={!models.includes(model || models[0])}>提交生成任务</Button>
        </Form>}
    </section>;
}
