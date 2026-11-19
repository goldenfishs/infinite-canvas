import { useEffect, useState } from "react";
import { Alert, App, Button, Empty, Form, Input, InputNumber, Select, Space, Spin, Switch, Table, Tabs, Tag } from "antd";
import { HardDrive, Layers3, RefreshCw, Settings2, Users } from "lucide-react";
import { Link } from "react-router-dom";
import { useLumiviaAccount } from "@/components/layout/lumivia-account";
import { formatBytes } from "@/lib/image-utils";
import { AdminQuota } from "@/pages/storage";
import { canvasJobs, jobColor, jobLabels, type JobRow } from "@/services/api/canvas-jobs";
import { cloudStorage, type CanvasPolicy, type CanvasSettings, type CosSettings } from "@/services/api/cloud-storage";

type PolicyForm = Omit<CanvasPolicy, "chatGroups"> & { chatGroups: string[]; cos?: CosSettings };
const videoProtocols: [string, string][] = [["Seedance 原生任务", "seedance"], ["OpenAI 兼容表单", "openai"], ["MiniMax-H3 JSON", "minimax"]];
const bytes = (value: number | null | undefined) => value === null || value === undefined ? "—" : formatBytes(value) || "0 B";
const opts = (elements: [string, string][]) => elements.map(([label, value]) => ({ label, value }));

export default function OperationsPage() {
    const { account, syncing } = useLumiviaAccount();
    return <div className="h-full overflow-y-auto"><div className="mx-auto max-w-6xl px-4 py-7 sm:px-8 sm:py-10">
        <div className="mb-5 flex flex-wrap items-end justify-between gap-3"><div><div className="mb-1 text-xs text-muted-foreground">乐米画布 / 管理员</div><h1 className="text-2xl font-semibold sm:text-3xl">画布运营后台</h1>
            <p className="mt-2 text-sm text-muted-foreground">管理生成任务、渠道、配额和素材存储。模型费用仍由乐米主站结算。</p></div>
            <Link to="/jobs" className="rounded-md border border-border px-3 py-1.5 text-sm">返回工作台</Link></div>
        {account?.isAdmin ? <Tabs key={account.id} destroyOnHidden items={[
            { key: "overview", label: <span className="flex items-center gap-2"><Layers3 size={16} />任务与资源</span>, children: <QueueOverview /> },
            { key: "policy", label: <span className="flex items-center gap-2"><Settings2 size={16} />系统设置</span>, children: <Settings /> },
            { key: "users", label: <span className="flex items-center gap-2"><Users size={16} />用户配额</span>, children: <AdminQuota /> },
        ]} /> : syncing ? <Spin /> : <Empty description="此页面仅乐米主站管理员可访问" />}
    </div></div>;
}

function QueueOverview() {
    const [page, go] = useState(1);
    const [version, refresh] = useState(0);
    const [report, setReport] = useState<Awaited<ReturnType<typeof canvasJobs.overview>>>();
    const [settings, setSettings] = useState<CanvasSettings>();
    const [error, setError] = useState("");
    const [loading, setLoading] = useState(true);
    useEffect(() => {
        const request = new AbortController();
        setLoading(true);
        setError("");
        void Promise.all([canvasJobs.overview(page, request.signal), cloudStorage.settings(request.signal)]).then(([jobs, cfg]) => {
            if (!request.signal.aborted) { setReport(jobs); setSettings(cfg); }
        }).catch((failure) => { if (!request.signal.aborted) setError(failure.message); }).finally(() => { if (!request.signal.aborted) setLoading(false); });
        return () => request.abort();
    }, [page, version]);
    const open = report?.counts;
    const waiting = open ? (open.staging || 0) + (open.queued || 0) + (open.submitting || 0) + (open.waiting || 0) : "—";
    return <section>
        <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
            {[["在途任务", waiting, `正在处理 ${report?.activeWorkers ?? "—"} / ${settings?.policy.workers ?? "—"}`],
              ["需要核对", open?.review ?? "—", `已完成 ${open?.completed ?? "—"} · 未完成 ${open?.failed ?? "—"}`],
              ["素材占用", bytes(settings?.totalUsed), `任务预留 ${bytes(settings?.reserved)} · 待清理 ${settings?.cleanupPending ?? "—"} 件`],
              ["画布进程内存", bytes(report?.memoryBytes), `${report?.memoryPaused ? "已暂停接收新任务 · " : ""}磁盘剩余 ${bytes(settings?.diskFree)}`]].map(([label, value, note]) => <div key={label} className="rounded-xl border border-border bg-card p-4">
                <div className="text-xs text-muted-foreground">{label}</div><div className="mt-2 text-xl font-semibold tabular-nums">{value}</div><div className="mt-1 text-xs text-muted-foreground">{note}</div>
            </div>)}
        </div>
        {error && <Alert type="error" showIcon title={error} className="mb-4" />}
        <div className="mb-3 flex items-center justify-between gap-2"><h2 className="font-semibold">最近的任务</h2><Button size="small" icon={<RefreshCw size={14} />} loading={loading} onClick={() => refresh((v) => v + 1)}>刷新</Button></div>
        <Table size="small" rowKey="id" pagination={false} loading={loading} scroll={{ x: 900 }} dataSource={report?.items || []} columns={[
            { title: "任务", render: (_: unknown, row: JobRow) => <span title={row.id}>{row.id.slice(0, 10)}</span> },
            { title: "用户", dataIndex: "owner" }, { title: "模型", dataIndex: "model" },
            { title: "状态", render: (_: unknown, row: JobRow) => <Tag color={jobColor(row.status)}>{jobLabels[row.status]}</Tag> },
            { title: "上游任务", render: (_: unknown, row: JobRow) => row.external || "—" },
            { title: "时间", render: (_: unknown, row: JobRow) => new Date(row.created).toLocaleString() },
            { title: "处理提示", dataIndex: "message" },
        ]} />
        <Space className="mt-4"><Button disabled={page <= 1 || loading} onClick={() => go(page - 1)}>上一页</Button><span>第 {page} 页</span><Button disabled={!report?.more || loading} onClick={() => go(page + 1)}>下一页</Button></Space>
        <p className="mt-3 text-xs text-muted-foreground">遇到「待核对」先按用户、时间或上游任务号查主站用量。画布不会自动重发已可能扣费的请求。</p>
    </section>;
}

function Settings() {
    const { message } = App.useApp();
    const [result, setResult] = useState<CanvasSettings>();
    const [error, setError] = useState("");
    const [version, refresh] = useState(0);
    const [saving, setSaving] = useState(false);
    useEffect(() => {
        const controller = new AbortController();
        setError("");
        void cloudStorage.settings(controller.signal).then(setResult).catch((failure) => { if (!controller.signal.aborted) setError(failure.message); });
        return () => controller.abort();
    }, [version]);
    if (!result) return error ? <Alert type="error" title={error} action={<Button onClick={() => refresh((v) => v + 1)}>重试</Button>} /> : <Spin />;
    async function save(values: PolicyForm) {
        if (!result || saving) return;
        const { cos, chatGroups, ...fields } = values;
        const changes = { ...result.policy, ...fields, chatGroups: (chatGroups || []).map(Number) };
        const candidate = cos?.bucket && (cos.secretId || cos.secretKey || cos.bucket !== result.cos?.bucket || cos.region !== result.cos?.region || (cos.domain || "") !== result.cos?.domain)
            ? { ...cos, domain: cos.domain || "" } : undefined;
        setSaving(true);
        setError("");
        try {
            await cloudStorage.saveSettings(result.revision, changes, candidate);
            message.success("设置已保存");
            refresh((v) => v + 1);
            void useLumiviaAccount.getState().sync();
        } catch (failure) { setError((failure as Error).message); }
        finally { setSaving(false); }
    }
    return <Form<PolicyForm> key={result.revision} layout="vertical" initialValues={{ ...result.policy, chatGroups: result.policy.chatGroups.map(String),
        cos: { region: result.cos?.region || "ap-hongkong", bucket: result.cos?.bucket || "", domain: result.cos?.domain || "" } }} onFinish={save} disabled={saving}>
        {error && <Alert type="error" title={error} showIcon className="mb-4" action={<Button size="small" onClick={() => refresh((v) => v + 1)}>读取当前设置</Button>} />}
        <div className="grid items-start gap-4 lg:grid-cols-2">
            <section className="rounded-xl border border-border bg-card p-4 sm:p-5">
                <h2 className="mb-4 flex items-center gap-2 font-semibold"><HardDrive size={17} />存储与保留</h2>
                <Form.Item name="backend" label="新素材的保存位置"><Select options={opts([["服务器本地磁盘", "local"], ["腾讯云 COS（私有桶）", "cos"]])} /></Form.Item>
                <div className="grid grid-cols-2 gap-x-3">
                    <NumberField name="retentionDays" label="保存期限（天）" min={0} max={3650} extra="0 为不过期，只影响新文件。" />
                    <NumberField name="totalLimitMiB" label="全站总容量（MiB）" min={1} max={1048576} />
                    <NumberField name="imageMiB" label="单图最大（MiB）" min={1} max={32} />
                    <NumberField name="videoMiB" label="单视频最大（MiB）" min={1} max={512} />
                    <NumberField name="diskReserveMiB" label="本机磁盘预留（MiB）" min={0} max={1048576} extra="COS 模式也需要暂存空间。" />
                </div>
                <details className="rounded-lg border border-border p-3">
                    <summary className="cursor-pointer font-medium">腾讯云 COS 连接 {result.cos ? <Tag color="green" className="ml-2">已校验</Tag> : <Tag className="ml-2">未配置</Tag>}</summary>
                    <div className="mt-3 grid gap-x-3 sm:grid-cols-2">
                        <Form.Item name={["cos", "bucket"]} label="存储桶完整名称"><Input placeholder="lumivia-canvas-APPID" spellCheck={false} /></Form.Item>
                        <Form.Item name={["cos", "region"]} label="地域"><Input placeholder="ap-hongkong" spellCheck={false} /></Form.Item>
                        <Form.Item className="sm:col-span-2" name={["cos", "domain"]} label="HTTPS 预览域名（可选）" extra="不填仍可直接下载，预览仅显示缩略图。"><Input placeholder="assets.lumivia.cn" spellCheck={false} /></Form.Item>
                        <Form.Item name={["cos", "secretId"]} label="SecretId"><Input.Password autoComplete="new-password" placeholder={result.cos ? "留空沿用" : "存储桶专用密钥"} /></Form.Item>
                        <Form.Item name={["cos", "secretKey"]} label="SecretKey"><Input.Password autoComplete="new-password" placeholder={result.cos ? "留空沿用" : "存储桶专用密钥"} /></Form.Item>
                    </div>
                    <p className="text-xs text-muted-foreground">保存前会上传一个测试文件，用签名链接下载并清理测试文件。新素材按选定位置保存，旧素材留在原处。</p>
                </details>
            </section>
            <div className="space-y-4">
                <section className="rounded-xl border border-border bg-card p-4 sm:p-5">
                    <h2 className="mb-3 font-semibold">模型与分组</h2>
                    <p className="mb-4 text-xs leading-5 text-muted-foreground">需要先在主站建好分组和模型价格；画布此处只控制入口和可用范围。<a className="ml-1 underline" href="https://api.lumivia.cn/admin/channels/pricing" target="_top">主站渠道定价</a></p>
                    <div className="grid grid-cols-3 gap-x-2">
                        <Form.Item name="imageEnabled" label="图片" valuePropName="checked"><Switch /></Form.Item>
                        <Form.Item name="videoEnabled" label="视频" valuePropName="checked"><Switch /></Form.Item>
                        <Form.Item name="chatEnabled" label="对话" valuePropName="checked"><Switch /></Form.Item>
                    </div>
                    <div className="grid grid-cols-2 gap-x-3">
                        <NumberField name="imageGroup" label="生图分组 ID" min={0} max={Number.MAX_SAFE_INTEGER} extra="0 沿用部署默认分组。" />
                        <Form.Item name="imageMode" label="生图上游模式"><Select options={opts([["自动（优先 BPS 异步）", "auto"], ["仅 BPS 异步", "native"], ["同步接口，由画布排队", "sync"]])} /></Form.Item>
                    </div>
                    <TagsField name="imageModels" label="图片模型白名单" hint="留空使用生图分组的 GPT Image 模型。" />
                    <div className="grid grid-cols-2 gap-x-3">
                        <NumberField name="videoGroup" label="视频分组 ID" min={0} max={Number.MAX_SAFE_INTEGER} />
                        <Form.Item name="videoProtocol" label="视频协议"><Select options={opts(videoProtocols)} /></Form.Item>
                    </div>
                    <TagsField name="videoModels" label="视频模型白名单" hint="开启视频时必填，需在分组模型列表中存在。" />
                    <Form.List name="extraVideoRoutes">{(routes, { add, remove }) => <div className="mb-4 space-y-2">
                        {routes.map((route) => <div key={route.key} className="grid grid-cols-2 gap-x-3 rounded-lg border border-border p-3">
                            <Form.Item className="col-span-2" name={[route.name, "model"]} label="额外视频模型" rules={[{ required: true }]}><Input placeholder="模型 ID" /></Form.Item>
                            <Form.Item name={[route.name, "group"]} label="分组 ID" rules={[{ required: true, type: "integer", min: 1 }]}><InputNumber min={1} precision={0} className="!w-full" /></Form.Item>
                            <Form.Item name={[route.name, "protocol"]} label="协议" rules={[{ required: true }]}><Select options={opts(videoProtocols)} /></Form.Item>
                            <Form.Item className="col-span-2 sm:col-span-1" name={[route.name, "resolutions"]} label="可选分辨率">
                                <Select mode="multiple" placeholder="使用协议默认值" options={["480p", "720p", "768p", "1080p", "1440p"].map((value) => ({ value }))} />
                            </Form.Item>
                            <Form.Item className="col-span-2 sm:col-span-1" name={[route.name, "durations"]} label="可选时长（秒）">
                                <Select mode="multiple" placeholder="使用全站时长范围" options={Array.from({ length: 12 }, (_, index) => ({ value: index + 4 }))} />
                            </Form.Item>
                            <Button size="small" type="text" danger onClick={() => remove(route.name)}>移除此项</Button>
                        </div>)}
                        <Button size="small" disabled={routes.length >= 12} onClick={() => add({ protocol: "openai" })}>添加额外视频渠道</Button>
                    </div>}</Form.List>
                    <NumberField name="videoSeconds" label="视频最长（秒）" min={4} max={15} />
                    <TagsField name="chatGroups" label="允许对话的分组 ID" hint="留空允许用户自己有权使用的分组。" />
                    <TagsField name="chatModels" label="对话模型白名单" hint="留空按用户现有密钥自动同步。" />
                </section>
                <section className="rounded-xl border border-border bg-card p-4 sm:p-5">
                    <h2 className="mb-3 font-semibold">队列与资源</h2>
                    <div className="grid grid-cols-2 gap-x-3">
                        <NumberField name="workers" label="并行处理数" min={1} max={4} extra="调大时，CPU、内存和带宽占用会增加。" />
                        <NumberField name="perUser" label="每账号在途上限" min={1} max={12} />
                        <NumberField name="queueSize" label="全站在途上限" min={1} max={300} />
                        <NumberField name="memoryGuardMiB" label="进程内存暂停阈值（MiB）" min={0} max={16384} extra="超过阈值时暂停受理和启动新任务，已提交任务继续查询。0 为关闭。" />
                    </div>
                    <TagsField name="resultHosts" label="上游素材域名白名单" hint="可填对象存储或上游 CDN 的完整主机名；留空只允许公网 HTTPS。" />
                </section>
            </div>
        </div>
        <div className="sticky bottom-0 mt-4 flex items-center justify-end gap-3 rounded-xl border border-border bg-background/95 p-3 text-sm">
            <span className="mr-auto text-xs text-muted-foreground">配置版本 {result.revision} · 配额降低不会自动删除旧文件</span>
            <Button onClick={() => refresh((v) => v + 1)} disabled={saving}>重新读取</Button><Button type="primary" htmlType="submit" loading={saving}>保存设置</Button>
        </div>
    </Form>;
}

function NumberField({ name, label, min, max, extra }: { name: keyof PolicyForm; label: string; min: number; max: number; extra?: string }) {
    return <Form.Item name={name} label={label} extra={extra} rules={[{ required: true, type: "integer", min, max, message: `请填 ${min} 到 ${max} 之间的整数` }]}>
        <InputNumber min={min} max={max} precision={0} className="!w-full" />
    </Form.Item>;
}

function TagsField({ name, label, hint }: { name: keyof PolicyForm; label: string; hint: string }) {
    return <Form.Item name={name} label={label} extra={hint}><Select mode="tags" tokenSeparators={[",", "，", " "]} maxCount={40} placeholder="输入后按回车，可填多个" options={[]} /></Form.Item>;
}
