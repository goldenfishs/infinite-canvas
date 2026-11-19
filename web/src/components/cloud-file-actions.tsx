import { useEffect, useRef, useState } from "react";
import { App, Button, Modal, Space } from "antd";
import { Download, Eye } from "lucide-react";
import { cloudStorage, type CloudFile } from "@/services/api/cloud-storage";

export const cloudFileName = (file: CloudFile) => `lumivia-${file.id.slice(0, 10)}.${file.mime === "image/jpeg" ? "jpg" : file.mime.split("/")[1]}`;

export function CloudFileActions({ file }: { file: CloudFile }) {
    const { message } = App.useApp();
    const [loading, setLoading] = useState("");
    const [link, setLink] = useState<{ url: string; preview: boolean; thumbnail: boolean; signedAt: number }>();
    const [broken, setBroken] = useState(false);
    const request = useRef<AbortController>(null);
    useEffect(() => () => request.current?.abort(), []);
    async function open(preview: boolean) {
        request.current?.abort();
        const controller = new AbortController();
        request.current = controller;
        setLoading(preview ? "preview" : "download");
        setBroken(false);
        try {
            const { url, previewQuality } = await cloudStorage.access(file.id, preview, controller.signal);
            if (controller.signal.aborted) return;
            setLink({ url, preview, thumbnail: previewQuality === "thumbnail", signedAt: Date.now() });
        } catch (error) {
            if (!controller.signal.aborted) message.error((error as Error).message);
        } finally {
            if (!controller.signal.aborted) setLoading("");
        }
    }
    return <>
        <Space size={4}>
            <Button size="small" type="text" icon={<Eye size={15} />} loading={loading === "preview"} onClick={() => void open(true)}>预览</Button>
            <Button size="small" type="text" icon={<Download size={15} />} loading={loading === "download"} onClick={() => void open(false)}>下载</Button>
        </Space>
        {link && <Modal open title={link.thumbnail ? "缩略图预览" : link.preview ? "原文件预览" : "下载原文件"} footer={null} onCancel={() => setLink(undefined)} width={link.preview ? 900 : 450}>
            {link.preview ? broken ? <div className="py-6 text-center text-muted-foreground">暂时无法加载原文件。<Button type="link" onClick={() => void open(true)}>刷新链接</Button></div>
                : file.mime.startsWith("video/")
                    ? <video className="mx-auto max-h-[68dvh] max-w-full" src={link.url} controls playsInline preload="metadata" onError={() => setBroken(true)} />
                    : <img src={link.url} referrerPolicy="no-referrer" alt="云端原图" className="mx-auto max-h-[70dvh] max-w-full object-contain" onError={() => setBroken(true)} />
                : <div className="space-y-4 py-2"><p className="text-sm text-muted-foreground">下载链接短期有效。链接失效时可重新获取。</p>
                    <Space wrap>
                        <Button type="primary" href={link.url} {...(link.url.startsWith("/lumivia/file/") ? { download: cloudFileName(file) } : { target: "_blank", rel: "noopener noreferrer" })} onClick={(event) => {
                            if (Date.now() - link.signedAt >= 235_000) { event.preventDefault(); void open(false); }
                        }}>下载 {cloudFileName(file)}</Button>
                        <Button type="link" loading={loading === "download"} onClick={() => void open(false)}>刷新链接</Button>
                    </Space></div>}
        </Modal>}
    </>;
}
