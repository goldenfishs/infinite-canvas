import { useEffect, useRef, useState } from "react";
import { App, Button, Empty, Input, InputNumber, Modal, Progress, Segmented, Space, Spin, Table, Tag } from "antd";
import { Download, Eye, Trash2, Upload } from "lucide-react";
import { lumiviaImageAccount, useLumiviaAccount } from "@/components/layout/lumivia-account";
import { cloudStorage, GIB, type CanvasUser, type CloudAdmin, type CloudFile, type CloudFiles } from "@/services/api/cloud-storage";
import { formatBytes as displayBytes } from "@/lib/image-utils";

const formatBytes = (bytes: number) => displayBytes(bytes) || "0 B";

export default function StoragePage() {
    const { account, syncing } = useLumiviaAccount();
    return <div className="h-full overflow-y-auto"><div className="mx-auto max-w-5xl px-4 py-8 sm:px-8">
        <h1 className="text-2xl font-semibold">云图库</h1>
        <p className="mt-2 mb-6 text-sm text-muted-foreground">在生图结果中选择「存云端」，也可以在这里上传。打开列表只读取文件信息，预览或下载时才加载原图。</p>
        {account ? <Library key={account.id} admin={account.isAdmin === true} /> : syncing ? <Spin /> : <Empty description="请先登录乐米主站，再打开画布" />}
    </div></div>;
}

function Library({ admin }: { admin: boolean }) {
    const [tab, setTab] = useState("images");
    return <>
        {admin && <Segmented className="mb-5" value={tab} onChange={setTab} options={[{ label: "我的图片", value: "images" }, { label: "存储管理", value: "admin" }]} />}
        {admin && tab === "admin" ? <AdminQuota /> : <MyImages />}
    </>;
}

function useRequestScope() {
    const scope = useRef<AbortController | null>(null);
    useEffect(() => {
        const controller = new AbortController();
        scope.current = controller;
        return () => controller.abort();
    }, []);
    return () => scope.current!.signal;
}

function Pager({ page, more, change }: { page: number; more?: boolean; change: (page: number) => void }) {
    return <Space className="mt-4">
        <Button disabled={page <= 1} onClick={() => change(page - 1)}>上一页</Button>
        <span>第 {page} 页</span>
        <Button disabled={!more} onClick={() => change(page + 1)}>下一页</Button>
    </Space>;
}

function MyImages() {
    const { message, modal } = App.useApp();
    const signal = useRequestScope();
    const uploadInput = useRef<HTMLInputElement>(null);
    const [uploading, setUploading] = useState(false);
    const [page, setPage] = useState(1);
    const [version, setVersion] = useState(0);
    const [result, setResult] = useState<CloudFiles>();
    const [loading, setLoading] = useState(true);
    const [working, setWorking] = useState<string>();
    const [preview, setPreview] = useState<string>();
    useEffect(() => {
        const controller = new AbortController();
        setLoading(true);
        void cloudStorage.list(page, controller.signal).then(setResult).catch((error) => {
            if (!controller.signal.aborted) message.error(error.message);
        }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
        return () => controller.abort();
    }, [page, version, message]);
    useEffect(() => () => { if (preview) URL.revokeObjectURL(preview); }, [preview]);
    const open = async (item: CloudFile, download: boolean) => {
        const active = signal();
        setWorking(item.id);
        try {
            const blob = await cloudStorage.read(item.id, active);
            if (active.aborted) return;
            if (download) {
                const url = URL.createObjectURL(blob);
                const link = Object.assign(document.createElement("a"), { href: url, download: fileName(item), hidden: true });
                document.body.append(link);
                try { link.click(); } finally {
                    link.remove();
                    window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
                }
            } else setPreview(URL.createObjectURL(blob));
        } catch (error) { if (!active.aborted) message.error((error as Error).message); }
        finally { setWorking(undefined); }
    };
    return <>
        <div className="mb-5 rounded-xl border border-border p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div><h2 className="font-medium">我的云存储</h2><p className="text-sm text-muted-foreground">{result ? `已使用 ${formatBytes(result.used)} / ${formatBytes(result.quota)}` : "正在读取用量"}</p></div>
                <Button icon={<Upload size={16} />} loading={uploading} disabled={!result || result.used >= result.quota} onClick={() => uploadInput.current?.click()}>上传图片</Button>
                <input ref={uploadInput} hidden type="file" accept="image/png,image/jpeg,image/webp,image/gif,image/avif" onChange={async (event) => {
                    const file = event.currentTarget.files?.[0];
                    event.currentTarget.value = "";
                    if (!file) return;
                    setUploading(true);
                    try {
                        await cloudStorage.upload(file, lumiviaImageAccount());
                        message.success("已存入云图库");
                        setPage(1);
                        setVersion((value) => value + 1);
                    } catch (error) { message.error((error as Error).message); }
                    finally { setUploading(false); }
                }} />
            </div>
            {result && <Progress className="mt-3" percent={result.quota ? Math.min(100, Math.round(result.used / result.quota * 100)) : 100} status={result.used >= result.quota ? "exception" : "normal"} />}
        </div>
        <Table rowKey="id" size="small" pagination={false} loading={loading} dataSource={result?.items ?? []} scroll={{ x: 560 }} columns={[
            { title: "文件", render: (_: unknown, item: CloudFile) => fileName(item) },
            { title: "大小", dataIndex: "bytes", render: formatBytes },
            { title: "保存时间", dataIndex: "created", render: (value: number) => new Date(value).toLocaleString() },
            { title: "操作", render: (_: unknown, item: CloudFile) => <Space>
                <Button size="small" icon={<Eye size={14} />} loading={working === item.id} disabled={!!working} onClick={() => void open(item, false)}>预览</Button>
                <Button size="small" icon={<Download size={14} />} disabled={!!working} onClick={() => void open(item, true)}>下载</Button>
                <Button size="small" danger icon={<Trash2 size={14} />} onClick={() => {
                    const active = signal();
                    modal.confirm({ title: "移除此云端图片？", content: "移除后会释放已占用的云端额度。", okText: "删除", cancelText: "取消", okButtonProps: { danger: true }, onOk: async () => {
                        try {
                            await cloudStorage.delete(item.id, active);
                            if (page > 1 && result?.items.length === 1) setPage(page - 1);
                            else setVersion((value) => value + 1);
                        } catch (error) { if (!active.aborted) { message.error((error as Error).message); throw error; } }
                    } });
                }}>删除</Button>
            </Space> },
        ]} />
        <Pager page={page} more={result?.more && !loading} change={setPage} />
        <Modal open={!!preview} title="原图预览" footer={null} onCancel={() => setPreview(undefined)} width={900}>
            {preview && <img src={preview} alt="云端原图" className="mx-auto max-h-[70dvh] max-w-full object-contain" />}
        </Modal>
    </>;
}

function fileName(item: CloudFile) {
    return `lumivia-${item.id.slice(0, 10)}.${item.mime === "image/jpeg" ? "jpg" : item.mime.split("/")[1]}`;
}

function AdminQuota() {
    const { message } = App.useApp();
    const signal = useRequestScope();
    const [page, setPage] = useState(1);
    const [search, setSearch] = useState("");
    const [version, setVersion] = useState(0);
    const [result, setResult] = useState<CloudAdmin>();
    const [loading, setLoading] = useState(true);
    const [editing, setEditing] = useState<CanvasUser | "default" | null>(null);
    const [quota, setQuota] = useState<number | null>(null);
    const [saving, setSaving] = useState(false);
    useEffect(() => {
        const controller = new AbortController();
        setLoading(true);
        void cloudStorage.adminList(page, search, controller.signal).then(setResult).catch((error) => {
            if (!controller.signal.aborted) message.error(error.message);
        }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
        return () => controller.abort();
    }, [page, search, version, message]);
    const save = async (inherit = false) => {
        if (!editing || (!inherit && (quota === null || quota < 0 || quota > 1024))) return;
        const active = signal();
        setSaving(true);
        try {
            const bytes = Math.round((quota ?? 0) * GIB);
            if (editing === "default") await cloudStorage.adminDefault(bytes, active);
            else await cloudStorage.adminUser(editing.id, inherit ? null : bytes, active);
            message.success("额度已更新");
            setEditing(null);
            setVersion((value) => value + 1);
        } catch (error) { if (!active.aborted) message.error((error as Error).message); }
        finally { setSaving(false); }
    };
    return <>
        <div className="mb-5 grid gap-3 sm:grid-cols-3">
            <div className="rounded-xl border border-border p-4"><div className="text-sm text-muted-foreground">全站默认额度</div><div className="mt-2 flex items-center justify-between gap-2"><strong>{result ? formatBytes(result.defaultQuota) : "—"}</strong><Button size="small" disabled={!result} onClick={() => { setQuota(result!.defaultQuota / GIB); setEditing("default"); }}>修改</Button></div></div>
            <div className="rounded-xl border border-border p-4"><div className="text-sm text-muted-foreground">图库总用量</div><div className="mt-2 font-semibold">{result ? `${formatBytes(result.totalUsed)} / ${formatBytes(result.totalLimit)}` : "—"}</div></div>
            <div className="rounded-xl border border-border p-4"><div className="text-sm text-muted-foreground">服务器磁盘可用</div><div className="mt-2 font-semibold">{result ? formatBytes(result.diskFree) : "—"}</div>{result && <div className="text-xs text-muted-foreground">预留 {formatBytes(result.reservedFree)} 给系统</div>}</div>
        </div>
        <Input.Search className="mb-4 max-w-sm" placeholder="按邮箱或用户 ID 搜索" onSearch={(value) => { setPage(1); setSearch(value.trim()); }} allowClear />
        <Table rowKey="id" size="small" pagination={false} loading={loading} dataSource={result?.items ?? []} scroll={{ x: 540 }} columns={[
            { title: "用户 ID", dataIndex: "id" }, { title: "邮箱", dataIndex: "email" },
            { title: "已使用", dataIndex: "used", render: formatBytes },
            { title: "存储额度", render: (_: unknown, user: CanvasUser) => <>{formatBytes(user.quota ?? result!.defaultQuota)} {user.quota === null && <Tag>默认</Tag>}</> },
            { title: "操作", render: (_: unknown, user: CanvasUser) => <Button size="small" onClick={() => { setEditing(user); setQuota((user.quota ?? result!.defaultQuota) / GIB); }}>调整额度</Button> },
        ]} />
        <Pager page={page} more={result?.more && !loading} change={setPage} />
        <p className="mt-4 text-xs text-muted-foreground">列表包含已进入过画布的主站账号。降低额度会保留现有图片，超额时暂停新增。</p>
        <Modal open={editing !== null} title={editing === "default" ? "调整全站默认额度" : "调整用户存储额度"} okText="保存" cancelText="取消" confirmLoading={saving} okButtonProps={{ disabled: quota === null || quota < 0 || quota > 1024 }} onOk={() => void save()} onCancel={() => { if (!saving) setEditing(null); }}>
            <Space direction="vertical" className="py-2">
                <InputNumber aria-label="存储额度（GiB）" value={quota} onChange={setQuota} min={0} max={1024} step={0.25} addonAfter="GiB" />
                <span className="text-xs text-muted-foreground">设为 0 后禁止新增图片。</span>
                {editing && editing !== "default" && <Button size="small" disabled={saving} onClick={() => void save(true)}>恢复全站默认额度</Button>}
            </Space>
        </Modal>
    </>;
}
