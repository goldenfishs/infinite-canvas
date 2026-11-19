import { Link } from "react-router-dom";
import { ArrowLeft, ArrowRight, BookOpen, CheckSquare, ClipboardPaste, CloudUpload, Download, FolderPlus, History, ImagePlus, LoaderCircle, PenLine, Plus, SlidersHorizontal, Sparkles, Trash2, Upload } from "lucide-react";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { App, Button, Checkbox, Drawer, Empty, Image, Input, Modal, Tag, Tooltip, Typography } from "antd";
import localforage from "localforage";
import { saveAs } from "file-saver";
import { useTranslation } from "react-i18next";

import { ImageSettingsPanel } from "@/components/image-settings-panel";
import { CloudFileActions } from "@/components/cloud-file-actions";
import { ModelPicker } from "@/components/model-picker";
import { lumiviaImageAccount, useLumiviaAccount } from "@/components/layout/lumivia-account";
import { PromptSelectDialog } from "@/components/prompts/prompt-select-dialog";
import { AssetPickerModal, type InsertAssetPayload } from "@/components/canvas/asset-picker-modal";
import { canvasThemes } from "@/lib/canvas-theme";
import { imageReferenceLabel } from "@/lib/image-reference-prompt";
import { modelOptionLabel, useConfigStore, useEffectiveConfig, type AiConfig } from "@/stores/use-config-store";
import { useThemeStore } from "@/stores/use-theme-store";
import { nanoid } from "nanoid";
import { dataUrlToFile, formatBytes, formatDuration, getDataUrlByteSize } from "@/lib/image-utils";
import { cloudStorage, type CloudFile } from "@/services/api/cloud-storage";
import type { CanvasJob } from "@/services/api/canvas-jobs";
import { getThumbnailDimensions } from "@/lib/image-thumbnail";
import { requestEdit, requestGeneration, requestHostedImages } from "@/services/api/image";
import { blobToDataUrl, deleteStoredImages, ensureImagePreview, fetchImageBlob, getImageBlob, getImagePreviewRevision, imageToDataUrl, previewUrlFor, resolveImageUrl, subscribeImagePreviews, uploadImage } from "@/services/image-storage";
import { useAssetStore } from "@/stores/use-asset-store";
import { useWorkbenchAgentStore } from "@/stores/use-workbench-agent-store";
import type { ReferenceImage } from "@/types/image";
import i18n from "@/i18n";

type GeneratedImage = {
    id: string;
    dataUrl: string;
    storageKey?: string;
    durationMs: number;
    width: number;
    height: number;
    bytes: number;
    mimeType?: string;
};

type GenerationResult = {
    id: string;
    status: "pending" | "success" | "failed";
    image?: GeneratedImage;
    error?: string;
};

type GenerationLog = {
    id: string;
    createdAt: number;
    title: string;
    prompt: string;
    time: string;
    model: string;
    config: GenerationLogConfig;
    references: ReferenceImage[];
    durationMs: number;
    successCount: number;
    failCount: number;
    imageCount: number;
    size: string;
    quality: string;
    status: "success" | "failed";
    images: GeneratedImage[];
    localSave?: "pending" | "failed" | "links";
};

type LogSummary = Omit<GenerationLog, "references" | "images"> & { thumbnail?: string };

type GenerationLogConfig = Pick<AiConfig, "model" | "imageModel" | "quality" | "size" | "count">;

type UpdateAiConfig = <K extends keyof AiConfig>(key: K, value: AiConfig[K]) => void;

const LOCAL_SAVE_NOTICE_MS = 12_000;
const RESULT_ACTION_BUTTON_CLASS = "min-w-0 px-1.5 [&_.ant-btn-icon]:shrink-0 [&>span:last-child]:min-w-0 [&>span:last-child]:truncate";
const logStore = localforage.createInstance({ name: "infinite-canvas", storeName: "image_generation_logs" });

const logIndex = localforage.createInstance({ name: "infinite-canvas", storeName: "image_generation_log_index" });
const logThumbnails = localforage.createInstance({ name: "infinite-canvas", storeName: "image_generation_log_thumbnails" });
const LOG_PAGE_SIZE = 20;
const LEGACY_BATCH_SIZE = 8;

export default function ImagePage() {
    const { message } = App.useApp();
    const { t } = useTranslation();
    useSyncExternalStore(subscribeImagePreviews, getImagePreviewRevision);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const dragDepthRef = useRef(0);
    const config = useConfigStore((state) => state.config);
    const effectiveConfig = useEffectiveConfig();
    const updateConfig = useConfigStore((state) => state.updateConfig);
    const isAiConfigReady = useConfigStore((state) => state.isAiConfigReady);
    const openConfigDialog = useConfigStore((state) => state.openConfigDialog);
    const addAsset = useAssetStore((state) => state.addAsset);
    const [prompt, setPrompt] = useState("");
    const [references, setReferences] = useState<ReferenceImage[]>([]);
    const [results, setResults] = useState<GenerationResult[]>([]);
    const [cloudJob, setCloudJob] = useState<CanvasJob>();
    const cloudRequest = useRef<AbortController>(null);
    const [logs, setLogs] = useState<LogSummary[]>([]);
    const [visibleCount, setVisibleCount] = useState(LOG_PAGE_SIZE);
    const [legacyKeys, setLegacyKeys] = useState<string[]>([]);
    const [legacyLoading, setLegacyLoading] = useState(false);
    const pendingLogs = useRef(new Map<string, GenerationLog>());
    const previewVersion = useRef(0);
    const visibleLogs = logs.slice(0, visibleCount);
    const [running, setRunning] = useState(false);
    const busy = useRef(false);
    const removedLogs = useRef(new Set<string>());
    const [logsOpen, setLogsOpen] = useState(false);
    const [settingsOpen, setSettingsOpen] = useState(false);
    const [promptDialogOpen, setPromptDialogOpen] = useState(false);
    const [assetPickerOpen, setAssetPickerOpen] = useState(false);
    const [startedAt, setStartedAt] = useState(0);
    const [elapsedMs, setElapsedMs] = useState(0);
    const [selectedLogIds, setSelectedLogIds] = useState<string[]>([]);
    const [previewLog, setPreviewLog] = useState<GenerationLog | null>(null);
    const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
    const [isReferenceDragActive, setIsReferenceDragActive] = useState(false);
    const [autoRunToken, setAutoRunToken] = useState(0);
    const imageCommand = useWorkbenchAgentStore((state) => state.imageCommand);
    const clearImageCommand = useWorkbenchAgentStore((state) => state.clearImageCommand);
    const updateAgentTask = useWorkbenchAgentStore((state) => state.updateTask);
    const processedCommandRef = useRef(0);
    const agentTaskIdRef = useRef<string | undefined>(undefined);

    const model = effectiveConfig.imageModel || effectiveConfig.model;
    const checkingAccount = useLumiviaAccount((state) => !state.account && state.syncing);
    const accountId = useLumiviaAccount((state) => state.account?.id);
    const signedIn = accountId !== undefined;
    const accountError = useLumiviaAccount((state) => state.error);
    const canGenerate = Boolean(prompt.trim()) && isAiConfigReady(effectiveConfig, model);
    const generationCount = Math.max(1, Math.min(4, Math.floor(Number(config.count) || 1)));

    useEffect(() => {
        setCloudJob(undefined);
        return () => cloudRequest.current?.abort();
    }, [accountId]);

    useEffect(() => {
        if (!running || !startedAt) return;
        const timer = window.setInterval(() => setElapsedMs(performance.now() - startedAt), 1000);
        return () => window.clearInterval(timer);
    }, [running, startedAt]);

    useEffect(() => {
        let mounted = true;
        void readLogIndex().then(({ saved, legacy }) => {
            if (!mounted) return;
            setLogs((current) => [...current, ...saved.filter((log) => !removedLogs.current.has(log.id) && !current.some((item) => item.id === log.id))]);
            setLegacyKeys(legacy);
        }).catch(() => { if (mounted) message.warning("暂时无法读取本机历史列表"); });
        return () => { mounted = false; };
    }, [message]);

    const loadLegacy = async () => {
        setLegacyLoading(true);
        const batch = legacyKeys.slice(0, LEGACY_BATCH_SIZE);
        const summaries: LogSummary[] = [];
        try {
            // Old IndexedDB rows contain the original inline. Touch one at a time,
            // only after explicitly opening old history, and keep only metadata.
            for (const id of batch) {
                const log = await logStore.getItem<GenerationLog>(id);
                if (!log || removedLogs.current.has(id)) continue;
                const summary = summarizeLog(log, id);
                await logIndex.setItem(id, summary);
                summaries.push(summary);
            }
            setLegacyKeys((keys) => keys.slice(batch.length));
            setVisibleCount((count) => count + summaries.length);
            setLogs((current) => [...current.filter((item) => !summaries.some((saved) => saved.id === item.id)), ...summaries.filter((item) => !removedLogs.current.has(item.id))].sort(newestFirst));
        } catch {
            message.warning("暂时无法读取较早的本机记录");
        } finally { setLegacyLoading(false); }
    };

    const addReferences = async (files?: FileList | null) => {
        const imageFiles = Array.from(files || []).filter((file) => file.type.startsWith("image/"));
        const nextReferences = await Promise.all(
            imageFiles.map(async (file) => {
                const image = await uploadImage(file);
                return { id: nanoid(), name: file.name, type: image.mimeType, dataUrl: image.url, storageKey: image.storageKey };
            }),
        );
        setReferences((value) => [...value, ...nextReferences]);
    };

    const addReferencesFromClipboard = async () => {
        try {
            const items = await navigator.clipboard.read();
            const blobs = await Promise.all(items.flatMap((item) => item.types.filter((type) => type.startsWith("image/")).map((type) => item.getType(type))));
            if (!blobs.length) {
                message.error(t("imageWorkbench.clipboardEmpty"));
                return;
            }
            const nextReferences = await Promise.all(
                blobs.map(async (blob, index) => {
                    const image = await uploadImage(blob);
                    return { id: nanoid(), name: `clipboard-${index + 1}.png`, type: image.mimeType, dataUrl: image.url, storageKey: image.storageKey };
                }),
            );
            setReferences((value) => [...value, ...nextReferences]);
            message.success(t("imageWorkbench.clipboardAdded", { count: nextReferences.length }));
        } catch {
            message.error(t("imageWorkbench.clipboardEmpty"));
        }
    };

    const generate = async () => {
        const agentTaskId = agentTaskIdRef.current;
        agentTaskIdRef.current = undefined;
        if (busy.current) {
            if (agentTaskId) updateAgentTask(agentTaskId, { status: "failed", error: t("imageWorkbench.busy") });
            return;
        }
        const text = prompt.trim();
        if (!text) {
            message.error(t("imageWorkbench.promptRequired"));
            if (agentTaskId) updateAgentTask(agentTaskId, { status: "failed", error: t("imageWorkbench.promptRequired") });
            return;
        }
        if (!isAiConfigReady(effectiveConfig, model)) {
            message.warning(t("workbench.configFirst"));
            openConfigDialog(true);
            if (agentTaskId) updateAgentTask(agentTaskId, { status: "failed", error: t("imageWorkbench.configIncomplete") });
            return;
        }

        const snapshot = buildRequestSnapshot();
        if (!snapshot) {
            if (agentTaskId) updateAgentTask(agentTaskId, { status: "failed", error: t("imageWorkbench.invalidParams") });
            return;
        }

        busy.current = true;
        setElapsedMs(0);
        setRunning(true);
        if (agentTaskId) updateAgentTask(agentTaskId, { status: "running", error: undefined });
        previewVersion.current++;
        setPreviewLog(null);
        setCloudJob(undefined);
        const slots = Array.from({ length: generationCount }, () => ({ id: nanoid(), status: "pending" as const }));
        setResults(slots);
        const batchStartedAt = performance.now();
        setStartedAt(batchStartedAt);

        if (signedIn) {
            const controller = new AbortController();
            cloudRequest.current = controller;
            try {
                const task = await requestHostedImages({ ...snapshot.config, count: String(generationCount) }, snapshot.text, snapshot.references, { signal: controller.signal });
                setResults([]);
                setCloudJob(task);
                if (agentTaskId) updateAgentTask(agentTaskId, { status: task.assets.length ? "succeeded" : "failed", successCount: task.assets.length,
                    failCount: generationCount - task.assets.length, error: task.assets.length ? undefined : task.message });
                if (task.assets.length) message.success(t("imageWorkbench.generated"));
            } catch (error) {
                if (controller.signal.aborted) return;
                setResults([]);
                if (agentTaskId) updateAgentTask(agentTaskId, { status: "failed", error: (error as Error).message });
                message.error((error as Error).message);
            } finally {
                busy.current = false;
                setRunning(false);
            }
            return;
        }

        const tasks = slots.map((slot) => runGenerationSlot(slot.id, snapshot));

        const result = await Promise.allSettled(tasks);
        const successImages = result.flatMap((item) => item.status === "fulfilled" ? item.value : []);
        const successCount = successImages.length;
        const failCount = result.filter((item) => item.status === "rejected").length;
        const failed = result.find((item): item is PromiseRejectedResult => item.status === "rejected");
        const error = failed?.reason instanceof Error ? failed.reason.message : failCount ? t("workbench.generationFailed") : undefined;
        if (agentTaskId) updateAgentTask(agentTaskId, { status: successCount ? "succeeded" : "failed", successCount, failCount, error: successCount ? undefined : error });

        try {
            saveLog(
                buildLog({
                    prompt: text,
                    model,
                    config: { ...snapshot.config, count: String(generationCount) },
                    references: snapshot.references,
                    durationMs: performance.now() - batchStartedAt,
                    successCount,
                    failCount,
                    status: successCount ? "success" : "failed",
                    images: successImages,
                }),
            );
            successCount ? message.success(t("imageWorkbench.generated")) : message.error(failed?.reason instanceof Error ? failed.reason.message : t("workbench.generationFailed"));
        } finally {
            busy.current = false;
            setRunning(false);
        }
    };

    // Handle image-generation commands from the Agent panel by setting the prompt and optionally starting generation.
    useEffect(() => {
        if (!imageCommand || imageCommand.nonce === processedCommandRef.current) return;
        processedCommandRef.current = imageCommand.nonce;
        clearImageCommand();
        if (typeof imageCommand.prompt === "string") setPrompt(imageCommand.prompt);
        if (imageCommand.run && running) {
            if (imageCommand.taskId) updateAgentTask(imageCommand.taskId, { status: "failed", error: t("imageWorkbench.busy") });
            return;
        }
        if (imageCommand.run) {
            agentTaskIdRef.current = imageCommand.taskId;
            setAutoRunToken((value) => value + 1);
        }
    }, [imageCommand, clearImageCommand, running, updateAgentTask]);

    useEffect(() => {
        if (!autoRunToken) return;
        void generate();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [autoRunToken]);

    const downloadImage = (image: GeneratedImage, index: number) => {
        const mimeType = image.dataUrl.match(/^data:(image\/[\w.+-]+);base64,/i)?.[1] || image.mimeType || "image/png";
        const filename = `image-${index + 1}.${mimeType.split("/")[1] || "png"}`;
        if (/^https?:\/\//i.test(image.dataUrl)) {
            saveAs(image.dataUrl, filename);
            return;
        }
        const link = document.createElement("a");
        let objectUrl: string | undefined;
        try {
            // Safari can ignore a data: download inside an embedded page. Hand it a Blob URL during the click.
            if (image.dataUrl.startsWith("data:")) {
                const file = dataUrlToFile({ id: image.id, name: filename, type: mimeType, dataUrl: image.dataUrl });
                if (!file.size) throw new Error(t("common.imageReadFailed"));
                objectUrl = URL.createObjectURL(file);
            } else if (!image.dataUrl.startsWith("blob:")) {
                throw new Error(t("common.imageReadFailed"));
            }
            link.href = objectUrl || image.dataUrl;
            link.download = filename;
            link.hidden = true;
            document.body.append(link);
            link.click();
        } catch {
            message.error(t("imageWorkbench.downloadFailed"));
        } finally {
            link.remove();
            if (objectUrl) {
                const url = objectUrl;
                window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
            }
        }
    };

    const addResultToReferences = async (image: GeneratedImage, index: number) => {
        try {
            // Reuse the original bytes without waiting for a local file or thumbnail write.
            const dataUrl = await imageToDataUrl(image, { signal: AbortSignal.timeout(20_000) });
            const type = dataUrl.match(/^data:(image\/[\w.+-]+);base64,/i)?.[1];
            if (!type) throw new Error(t("common.imageReadFailed"));
            const extension = type.split("/")[1];
            setReferences((value) => [...value, { id: nanoid(), name: `result-${index + 1}.${extension}`, type, dataUrl }]);
            message.success(t("imageWorkbench.addedReference"));
        } catch {
            message.error(t("imageWorkbench.addReferenceFailed"));
        }
    };

    const saveResultToAssets = async (image: GeneratedImage, index: number) => {
        const stored = await uploadImage(image.dataUrl);
        await addAsset({
            kind: "image",
            title: t("imageWorkbench.resultTitle", { count: index + 1 }),
            coverUrl: stored.url,
            tags: [],
            source: t("imageWorkbench.source"),
            data: { dataUrl: stored.url, storageKey: stored.storageKey, width: stored.width, height: stored.height, bytes: stored.bytes, mimeType: stored.mimeType },
            metadata: { source: "image-page", prompt },
        });
        message.success(t("common.addedToAssets"));
    };

    const insertPickedAsset = async (payload: InsertAssetPayload) => {
        if (payload.kind === "text") {
            setPrompt(payload.content);
        } else if (payload.kind === "image") {
            const stored = await uploadImage(payload.dataUrl);
            setReferences((value) => [...value, { id: nanoid(), name: payload.title, type: stored.mimeType, dataUrl: stored.url, storageKey: stored.storageKey }]);
        } else {
            message.warning(t("imageWorkbench.unsupportedAsset"));
        }
        setAssetPickerOpen(false);
    };

    const createSession = () => {
        previewVersion.current++;
        setPrompt("");
        setReferences([]);
        setResults([]);
        setElapsedMs(0);
        setStartedAt(0);
        setSelectedLogIds([]);
        setPreviewLog(null);
    };

    const deleteSelectedLogs = () => {
        previewVersion.current++;
        const ids = new Set(selectedLogIds);
        setLogs((current) => current.filter((log) => !ids.has(log.id)));
        for (const id of ids) removedLogs.current.add(id);
        void (async () => {
            for (const id of ids) {
                const log = await logStore.getItem<GenerationLog>(id);
                await logStore.removeItem(id);
                await logIndex.removeItem(id);
                await logThumbnails.removeItem(id);
                if (log) await deleteStoredImages(log.images.flatMap((image) => image.storageKey ? [image.storageKey] : []));
                pendingLogs.current.delete(id);
            }
        })().catch(() => message.warning(t("imageWorkbench.historyDeleteFailed")));
        if (previewLog && ids.has(previewLog.id)) {
            setPreviewLog(null);
            setResults([]);
        }
        setSelectedLogIds([]);
        setDeleteConfirmOpen(false);
    };

    const saveLog = (log: GenerationLog) => {
        pendingLogs.current.set(log.id, log);
        setLogs((current) => [summarizeLog(log), ...current]);
        const updateSummary = (changes: Partial<LogSummary>) => setLogs((current) => current.map((item) => item.id === log.id ? { ...item, ...changes } : item));
        const notice = window.setTimeout(() => updateSummary({ localSave: "pending" }), LOCAL_SAVE_NOTICE_MS);
        // Preserve inline responses; archive URL-only results while they are still available.
        void (async () => {
            const archived = await Promise.all(log.images.map(async (image) => {
                if (image.storageKey || image.dataUrl.startsWith("data:")) return image;
                try {
                    return { ...image, dataUrl: await imageToDataUrl(image, { signal: AbortSignal.timeout(30_000) }) };
                } catch { return image; }
            }));
            if (removedLogs.current.has(log.id)) return;
            const localSave = archived.some((image) => !image.storageKey && !image.dataUrl.startsWith("data:")) ? "links" as const : undefined;
            const full = { ...log, images: archived, localSave };
            await logStore.setItem(log.id, serializeLog(full));
            if (removedLogs.current.has(log.id)) return;
            const summary = summarizeLog(full);
            await logIndex.setItem(log.id, summary);
            pendingLogs.current.delete(log.id);
            window.clearTimeout(notice);
            updateSummary({ localSave });
            const image = archived[0];
            if (!image?.dataUrl || !/^(data:|blob:)/.test(image.dataUrl)) return;
            const thumbnail = await historyThumbnail(image).catch(() => undefined);
            if (thumbnail && !removedLogs.current.has(log.id)) {
                await logThumbnails.setItem(log.id, thumbnail);
                updateSummary({ thumbnail });
            }
        })().catch(() => updateSummary({ localSave: "failed" })).finally(() => window.clearTimeout(notice));
    };

    const previewGenerationLog = async (summary: LogSummary) => {
        const version = ++previewVersion.current;
        try {
            const raw = pendingLogs.current.get(summary.id) ?? await logStore.getItem<GenerationLog>(summary.id);
            if (!raw) throw new Error("找不到这条本机记录");
            const log = await normalizeLog(raw);
            setCloudJob(undefined);
            if (version !== previewVersion.current || removedLogs.current.has(summary.id)) return;
            setPreviewLog(log);
            setLogsOpen(false);
            setPrompt(log.prompt);
            setReferences(log.references);
            if (log.config.imageModel || log.model) updateConfig("imageModel", log.config.imageModel || log.model);
            if (log.config.quality) updateConfig("quality", log.config.quality);
            if (log.config.size) updateConfig("size", log.config.size);
            if (log.config.count) updateConfig("count", log.config.count);
            setResults(log.images.map((image) => ({ id: image.id, status: "success", image })));
        } catch (error) { if (version === previewVersion.current) message.error((error as Error).message || "无法打开本机记录"); }
    };

    const saveResultToCloud = async (image: GeneratedImage) => {
        try {
            const account = lumiviaImageAccount();
            const blob = await originalImageBlob(image);
            await cloudStorage.upload(blob, account);
            message.success("已存入云图库");
        } catch (error) { message.error((error as Error).message || "保存云端图片失败"); }
    };

    const buildRequestSnapshot = () => {
        const text = prompt.trim();
        if (!text) {
            message.error(t("imageWorkbench.promptRequired"));
            return null;
        }
        if (!isAiConfigReady(effectiveConfig, model)) {
            message.warning(t("workbench.configFirst"));
            openConfigDialog(true);
            return null;
        }
        return { text, config: { ...effectiveConfig, model, count: "1" }, references: [...references] };
    };

    const runGenerationSlot = async (slotId: string, snapshot: { text: string; config: AiConfig; references: ReferenceImage[] }) => {
        const itemStartedAt = performance.now();
        try {
            const result = snapshot.references.length ? await requestEdit(snapshot.config, snapshot.text, snapshot.references) : await requestGeneration(snapshot.config, snapshot.text);
            const durationMs = performance.now() - itemStartedAt;
            const images: GeneratedImage[] = result.filter(({ dataUrl }) => /^https?:\/\/|^data:image\/(?:png|jpeg|webp|gif|avif);base64,/i.test(dataUrl)).map(({ id, dataUrl }) => ({
                id, dataUrl, durationMs, width: 0, height: 0, bytes: dataUrl.startsWith("data:") ? getDataUrlByteSize(dataUrl) : 0,
            }));
            if (!images.length) throw new Error(t("imageWorkbench.missingResult"));
            setResults((value) => value.flatMap((slot) => slot.id === slotId ? images.map((image) => ({ id: image.id, status: "success" as const, image })) : [slot]));
            return images;
        } catch (error) {
            setResults((value) => value.map((slot) => slot.id === slotId ? { ...slot, status: "failed", error: error instanceof Error ? error.message : t("workbench.generationFailed") } : slot));
            throw error;
        }
    };

    const retryResult = async (slotId: string) => {
        if (busy.current) return;
        const snapshot = buildRequestSnapshot();
        if (!snapshot) return;
        busy.current = true;
        setRunning(true);
        setPreviewLog(null);
        setResults((value) => value.map((slot) => slot.id === slotId ? { id: slotId, status: "pending" } : slot));
        const retryStartedAt = performance.now();
        setStartedAt(retryStartedAt);
        setElapsedMs(0);
        let images: GeneratedImage[] = [];
        try {
            images = await runGenerationSlot(slotId, snapshot);
            message.success(t("workbench.retrySuccess"));
        } catch {
            // The slot already displays the request failure. Only a manual retry may regenerate it.
        } finally {
            busy.current = false;
            setRunning(false);
            saveLog(buildLog({ prompt: snapshot.text, model, config: snapshot.config, references: snapshot.references,
                durationMs: performance.now() - retryStartedAt, successCount: images.length, failCount: images.length ? 0 : 1,
                status: images.length ? "success" : "failed", images }));
        }
    };

    async function reuseCloudImage(file: CloudFile, index: number, save: boolean) {
        try {
            const blob = await cloudStorage.material(file.id, AbortSignal.timeout(60_000));
            const dataUrl = await blobToDataUrl(blob);
            const image = { id: file.id, dataUrl, mimeType: file.mime, durationMs: 0, width: 0, height: 0, bytes: blob.size };
            await (save ? saveResultToAssets(image, index) : addResultToReferences(image, index));
        } catch (error) {
            message.error((error as Error).message);
        }
    }

    return (
        <div className="flex h-full flex-col overflow-hidden bg-stone-50 text-stone-900 dark:bg-stone-950 dark:text-stone-100">
            <main className="grid min-h-0 flex-1 grid-cols-1 gap-3 overflow-y-auto p-3 lg:grid-cols-[300px_minmax(0,1fr)] lg:overflow-hidden xl:grid-cols-[320px_minmax(0,1fr)]">
                <aside className="thin-scrollbar hidden min-h-0 overflow-y-auto rounded-lg border border-stone-200 bg-card p-4 shadow-sm dark:border-stone-800 lg:block">
                    <LogPanel
                        logs={visibleLogs}
                        more={logs.length > visibleCount}
                        onMore={() => setVisibleCount((count) => count + LOG_PAGE_SIZE)}
                        legacyCount={legacyKeys.length}
                        legacyLoading={legacyLoading}
                        onLegacy={() => void loadLegacy()}
                        selectedLogIds={selectedLogIds}
                        activeLogId={previewLog?.id}
                        onSelectedLogIdsChange={setSelectedLogIds}
                        onCreateSession={createSession}
                        onDeleteSelected={() => setDeleteConfirmOpen(true)}
                        onPreviewLog={(log) => void previewGenerationLog(log)}
                    />
                </aside>

                <section className="grid gap-3 lg:min-h-0 lg:overflow-hidden xl:grid-cols-[420px_minmax(0,1fr)]">
                    <div className="thin-scrollbar flex flex-col rounded-lg border border-stone-200 bg-card p-4 shadow-sm dark:border-stone-800 lg:min-h-0 lg:overflow-y-auto">
                        <div>
                            <div className="flex items-start justify-between gap-3">
                                <div className="min-w-0">
                                    <div className="flex flex-wrap items-center gap-3"><h1 className="text-2xl font-semibold text-stone-950 dark:text-stone-100">{t("imageWorkbench.title")}</h1><Link to="/jobs" className="rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground hover:text-foreground">生成记录</Link></div>
                                </div>
                                <div className="flex shrink-0 gap-2 lg:hidden">
                                    <Button icon={<History className="size-4" />} onClick={() => setLogsOpen(true)}>
                                        {t("workbench.logs")}
                                    </Button>
                                    <Button icon={<SlidersHorizontal className="size-4" />} onClick={() => setSettingsOpen(true)}>
                                        {t("workbench.settings")}
                                    </Button>
                                </div>
                            </div>
                        </div>

                        <div className="mt-6 space-y-5">
                            <div>
                                <div className="mb-2 flex items-center justify-between gap-3">
                                    <span className="text-base font-semibold">{t("workbench.prompt")}</span>
                                    <div className="flex gap-2">
                                        <Button size="small" icon={<BookOpen className="size-3.5" />} onClick={() => setPromptDialogOpen(true)}>
                                            {t("workbench.viewPrompts")}
                                        </Button>
                                        <Button size="small" icon={<FolderPlus className="size-3.5" />} onClick={() => setAssetPickerOpen(true)}>
                                            {t("workbench.viewAssets")}
                                        </Button>
                                    </div>
                                </div>
                                <Input.TextArea value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={7} placeholder={t("imageWorkbench.promptPlaceholder")} />
                            </div>

                            <div className="min-w-0">
                                <div className="mb-2 flex items-center justify-between gap-3">
                                    <span className="text-base font-semibold">{t("imageWorkbench.references")}</span>
                                    <div className="flex gap-2">
                                        <Button size="small" icon={<ClipboardPaste className="size-3.5" />} onClick={() => void addReferencesFromClipboard()}>
                                            {t("workbench.clipboard")}
                                        </Button>
                                        <Button size="small" icon={<Upload className="size-3.5" />} onClick={() => fileInputRef.current?.click()}>
                                            {t("workbench.upload")}
                                        </Button>
                                    </div>
                                </div>
                                <div
                                    className={`hover-scrollbar hover-scrollbar-hint relative flex min-h-24 w-full min-w-0 max-w-full gap-2 overflow-x-scroll overflow-y-hidden rounded-lg border border-dashed p-2 pb-3 overscroll-x-contain transition-colors ${isReferenceDragActive ? "border-stone-900 bg-stone-100/80 dark:border-stone-100 dark:bg-stone-900/80" : "border-stone-300 dark:border-stone-700"}`}
                                    onDragEnter={(event) => {
                                        event.preventDefault();
                                        dragDepthRef.current += 1;
                                        if (event.dataTransfer.types.includes("Files")) setIsReferenceDragActive(true);
                                    }}
                                    onDragOver={(event) => {
                                        event.preventDefault();
                                        event.dataTransfer.dropEffect = "copy";
                                    }}
                                    onDragLeave={(event) => {
                                        event.preventDefault();
                                        dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
                                        if (!dragDepthRef.current) setIsReferenceDragActive(false);
                                    }}
                                    onDrop={(event) => {
                                        event.preventDefault();
                                        dragDepthRef.current = 0;
                                        setIsReferenceDragActive(false);
                                        void addReferences(event.dataTransfer.files);
                                    }}
                                    onWheel={(event) => {
                                        if (event.currentTarget.scrollWidth <= event.currentTarget.clientWidth) return;
                                        event.preventDefault();
                                        event.currentTarget.scrollLeft += event.deltaY;
                                    }}
                                >
                                    {references.map((item, index) => (
                                        <div key={item.id} className="group relative size-20 shrink-0 overflow-hidden rounded-md border border-stone-200 dark:border-stone-800">
                                            <img src={previewUrlFor(item.storageKey) || item.dataUrl} alt={item.name} className="size-full object-cover" />
                                            <span className="absolute left-1 top-1 rounded bg-black/60 px-1.5 py-0.5 text-[10px] font-medium text-white">{imageReferenceLabel(index)}</span>
                                            <ReferenceOrderButtons index={index} total={references.length} onMove={(offset) => setReferences((value) => moveListItem(value, index, offset))} />
                                            <button
                                                type="button"
                                                className="absolute right-1 top-1 hidden size-6 items-center justify-center rounded bg-black/60 text-white group-hover:flex"
                                                onClick={() => setReferences((value) => value.filter((ref) => ref.id !== item.id))}
                                                aria-label={t("imageWorkbench.removeReference")}
                                            >
                                                <Trash2 className="size-3.5" />
                                            </button>
                                        </div>
                                    ))}
                                    {!references.length ? <div className="flex min-w-full items-center justify-center text-sm text-stone-500">{isReferenceDragActive ? t("imageWorkbench.dropReferences") : t("imageWorkbench.noReferences")}</div> : null}
                                </div>
                            </div>

                            <div className="flex items-center justify-between rounded-lg border border-stone-200 bg-stone-50 px-3 py-2 text-sm dark:border-stone-800 dark:bg-stone-900 sm:hidden">
                                <span className="truncate text-stone-500 dark:text-stone-400">
                                    {modelOptionLabel(effectiveConfig, model)} · {effectiveConfig.size} · {effectiveConfig.quality}
                                </span>
                                <Button size="small" type="text" icon={<SlidersHorizontal className="size-4" />} onClick={() => setSettingsOpen(true)}>
                                    {t("workbench.adjust")}
                                </Button>
                            </div>

                            <div className="hidden gap-4 sm:grid sm:grid-cols-2">
                                <GenerationSettings config={effectiveConfig} model={model} updateConfig={updateConfig} openConfigDialog={openConfigDialog} />
                            </div>
                        </div>

                        <div className="mt-auto pt-6">
                            <Button type="primary" size="large" block icon={<Sparkles className="size-4" />} loading={running} disabled={!canGenerate || running} onClick={() => void generate()}>
                                {checkingAccount ? t("config.lumivia.checking") : t("workbench.generate")}
                            </Button>
                            {accountError && <p role="status" className="mt-2 text-sm text-stone-500">{accountError}</p>}
                        </div>
                    </div>

                    <div className="thin-scrollbar rounded-lg border border-stone-200 bg-card p-4 shadow-sm dark:border-stone-800 lg:min-h-0 lg:overflow-y-auto lg:p-5">
                        <div className="mb-4 flex items-center justify-between gap-3">
                            <div>
                                <h2 className="text-xl font-semibold">{t("workbench.results")}</h2>
                            </div>
                            {running ? <Tag className="m-0 px-2 py-1">{t("workbench.waiting", { time: formatDuration(elapsedMs) })}</Tag> : null}
                        </div>
                        {cloudJob ? <>
                            {cloudJob.message && <p role="status" className="mb-3 text-sm text-amber-600 dark:text-amber-400">{cloudJob.message}</p>}
                            <div className="grid gap-4 sm:grid-cols-2 2xl:grid-cols-3">
                                {cloudJob.assets.map((file, index) => <div key={file.id} className="overflow-hidden rounded-lg border border-border bg-background">
                                    {file.thumbUrl && <img src={file.thumbUrl} alt={`生成结果 ${index + 1}`} className="aspect-square w-full object-contain" loading="lazy" decoding="async" />}
                                    <div className="space-y-1 p-2 text-xs text-muted-foreground"><p>{formatBytes(file.bytes)} · 已存入云图库</p>
                                        <CloudFileActions file={file} />
                                        <div className="flex flex-wrap gap-1"><Button size="small" type="text" onClick={() => void reuseCloudImage(file, index, false)}>加入参考图</Button><Button size="small" type="text" onClick={() => void reuseCloudImage(file, index, true)}>存为画布素材</Button></div>
                                    </div>
                                </div>)}
                            </div>
                            <Link to="/jobs" className="mt-3 inline-flex text-sm text-muted-foreground hover:text-foreground">查看我的生成记录</Link>
                        </> : results.length ? (
                            <div className="grid gap-4 sm:grid-cols-2 2xl:grid-cols-3">
                                {results.map((result, index) =>
                                    result.status === "success" && result.image ? (
                                        <ResultImageCard key={result.id} image={result.image} index={index} onEdit={addResultToReferences} onDownload={downloadImage} onSaveAsset={saveResultToAssets} onSaveCloud={signedIn ? saveResultToCloud : undefined} />
                                    ) : result.status === "failed" ? (
                                        <FailedImageCard key={result.id} error={result.error || t("workbench.generationFailed")} disabled={running} onRetry={() => retryResult(result.id)} />
                                    ) : (
                                        <PendingImageCard key={result.id} />
                                    ),
                                )}
                            </div>
                        ) : (
                            <div className="flex min-h-[320px] flex-col items-center justify-center rounded-lg border border-dashed border-stone-300 text-center dark:border-stone-700 lg:min-h-[560px]">
                                <ImagePlus className="mb-4 size-11 text-stone-400" />
                                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("imageWorkbench.empty")} />
                            </div>
                        )}
                    </div>
                </section>
            </main>
            <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                multiple
                className="hidden"
                onChange={(event) => {
                    void addReferences(event.target.files);
                    event.target.value = "";
                }}
            />
            <Drawer title={t("workbench.logs")} placement="bottom" size="large" open={logsOpen} onClose={() => setLogsOpen(false)}>
                <LogPanel
                    logs={visibleLogs}
                    more={logs.length > visibleCount}
                    onMore={() => setVisibleCount((count) => count + LOG_PAGE_SIZE)}
                    legacyCount={legacyKeys.length}
                    legacyLoading={legacyLoading}
                    onLegacy={() => void loadLegacy()}
                    selectedLogIds={selectedLogIds}
                    activeLogId={previewLog?.id}
                    onSelectedLogIdsChange={setSelectedLogIds}
                    onCreateSession={createSession}
                    onDeleteSelected={() => setDeleteConfirmOpen(true)}
                    onPreviewLog={(log) => void previewGenerationLog(log)}
                />
            </Drawer>
            <Drawer title={t("workbench.settings")} placement="bottom" size="82vh" open={settingsOpen} onClose={() => setSettingsOpen(false)}>
                <div className="grid grid-cols-2 gap-3 pb-4">
                    <GenerationSettings config={effectiveConfig} model={model} updateConfig={updateConfig} openConfigDialog={openConfigDialog} />
                </div>
            </Drawer>
            <PromptSelectDialog open={promptDialogOpen} onOpenChange={setPromptDialogOpen} onSelect={setPrompt} />
            <AssetPickerModal open={assetPickerOpen} defaultTab="my-assets" onInsert={(payload) => void insertPickedAsset(payload)} onClose={() => setAssetPickerOpen(false)} />
            <Modal title={t("workbench.deleteLogs")} open={deleteConfirmOpen} onCancel={() => setDeleteConfirmOpen(false)} onOk={deleteSelectedLogs} okText={t("common.delete")} okButtonProps={{ danger: true }} cancelText={t("common.cancel")}>
                {t("workbench.deleteLogsConfirm", { count: selectedLogIds.length })}
            </Modal>
        </div>
    );
}

function GenerationSettings({ config, model, updateConfig, openConfigDialog }: { config: AiConfig; model: string; updateConfig: UpdateAiConfig; openConfigDialog: (shouldPromptContinue?: boolean) => void }) {
    const theme = canvasThemes[useThemeStore((state) => state.theme)];
    const { t } = useTranslation();

    return (
        <>
            <label className="col-span-2 block min-w-0 sm:col-span-1">
                <span className="mb-1.5 block text-sm font-semibold sm:mb-2 sm:text-base">{t("workbench.model")}</span>
                <ModelPicker config={config} value={model} onChange={(value) => updateConfig("imageModel", value)} capability="image" fullWidth onMissingConfig={() => openConfigDialog(false)} />
            </label>
            <div className="col-span-2">
                <ImageSettingsPanel config={config} onConfigChange={(key, value) => updateConfig(key, value)} theme={theme} showTitle={false} className="space-y-4" maxCount={4} />
            </div>
        </>
    );
}

function ResultImageCard({
    image,
    index,
    onEdit,
    onDownload,
    onSaveAsset,
    onSaveCloud,
}: {
    image: GeneratedImage;
    index: number;
    onEdit: (image: GeneratedImage, index: number) => Promise<void>;
    onDownload: (image: GeneratedImage, index: number) => void;
    onSaveAsset: (image: GeneratedImage, index: number) => void;
    onSaveCloud?: (image: GeneratedImage) => Promise<void>;
}) {
    const { t } = useTranslation();
    useSyncExternalStore(subscribeImagePreviews, getImagePreviewRevision);
    const [dimensions, setDimensions] = useState("");
    const [failed, setFailed] = useState(false);
    const [addingReference, setAddingReference] = useState(false);
    const [savingCloud, setSavingCloud] = useState(false);
    const addReference = async () => {
        setAddingReference(true);
        try {
            await onEdit(image, index);
        } finally {
            setAddingReference(false);
        }
    };
    return (
        <div className="overflow-hidden rounded-lg border border-stone-200 bg-background dark:border-stone-800" onLoadCapture={(event) => {
            const img = event.target;
            if (img instanceof HTMLImageElement && event.currentTarget.contains(img) && img.naturalWidth && img.naturalHeight) {
                setFailed(false);
                setDimensions(`${img.naturalWidth}x${img.naturalHeight}`);
            }
        }}>
            <Image src={previewUrlFor(image.storageKey) || image.dataUrl} preview={{ src: image.dataUrl }} onError={() => setFailed(true)} alt={t("imageWorkbench.resultAlt", { count: index + 1 })} className="aspect-square object-cover" />
            <div className="space-y-2 border-t border-stone-200 px-3 py-2.5 dark:border-stone-800">
                {failed && <div className="text-xs text-amber-600 dark:text-amber-400">{t("imageWorkbench.previewUnavailable")}</div>}
                <div className="flex min-w-0 gap-x-2 gap-y-1 text-xs text-stone-500 dark:text-stone-400">
                    <span>
                        {image.width && image.height ? `${image.width}x${image.height}` : dimensions}
                    </span>
                    <span>{formatBytes(image.bytes)}</span>
                    <span>{formatDuration(image.durationMs)}</span>
                </div>
                <div className="grid min-w-0 grid-cols-3 gap-2">
                    <Tooltip title={t("common.addToAssets")}>
                        <Button className={RESULT_ACTION_BUTTON_CLASS} size="small" icon={<FolderPlus className="size-3.5" />} onClick={() => void onSaveAsset(image, index)}>
                            {t("common.addToAssets")}
                        </Button>
                    </Tooltip>
                    <Tooltip title={t("imageWorkbench.addReference")}>
                        <Button className={RESULT_ACTION_BUTTON_CLASS} size="small" icon={<PenLine className="size-3.5" />} loading={addingReference} onClick={() => void addReference()}>
                            {t("imageWorkbench.addReference")}
                        </Button>
                    </Tooltip>
                    <Tooltip title={t("common.download")}>
                        <Button className={RESULT_ACTION_BUTTON_CLASS} size="small" icon={<Download className="size-3.5" />} onClick={() => onDownload(image, index)}>
                            {t("common.download")}
                        </Button>
                    </Tooltip>
                </div>
                {onSaveCloud && <Button size="small" icon={<CloudUpload className="size-3.5" />} loading={savingCloud} onClick={async () => {
                    setSavingCloud(true);
                    try { await onSaveCloud(image); } finally { setSavingCloud(false); }
                }}>存云端</Button>}
            </div>
        </div>
    );
}

function PendingImageCard() {
    const { t } = useTranslation();
    return (
        <div className="relative aspect-square overflow-hidden rounded-lg border border-dashed border-stone-300 bg-stone-50 dark:border-stone-700 dark:bg-stone-900">
            <div
                className="absolute inset-0 opacity-60"
                style={{
                    backgroundImage: "radial-gradient(circle, rgba(120,113,108,0.35) 1.4px, transparent 1.6px)",
                    backgroundSize: "16px 16px",
                }}
            />
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-sm text-stone-500 dark:text-stone-400">
                <LoaderCircle className="size-6 animate-spin" />
                <span>{t("workbench.generating")}</span>
            </div>
        </div>
    );
}

function FailedImageCard({ error, onRetry, disabled }: { error: string; onRetry: () => void; disabled: boolean }) {
    const { t } = useTranslation();
    return (
        <div className="overflow-hidden rounded-lg border border-red-200 bg-red-50 dark:border-red-950 dark:bg-red-950/20">
            <div className="flex aspect-square flex-col items-center justify-center gap-3 p-5 text-center">
                <div className="text-sm font-medium text-red-600 dark:text-red-300">{t("workbench.failed")}</div>
                <Typography.Paragraph ellipsis={{ rows: 4 }} className="!mb-0 !text-xs !text-red-500 dark:!text-red-300">
                    {error}
                </Typography.Paragraph>
            </div>
            <div className="flex justify-end border-t border-red-200 p-3 dark:border-red-950">
                <Button size="small" danger disabled={disabled} onClick={onRetry}>
                    {t("workbench.retry")}
                </Button>
            </div>
        </div>
    );
}

function LogPanel({
    logs,
    selectedLogIds,
    activeLogId,
    onSelectedLogIdsChange,
    onCreateSession,
    onDeleteSelected,
    onPreviewLog,
    more, onMore, legacyCount, legacyLoading, onLegacy,
}: {
    logs: LogSummary[];
    more: boolean;
    onMore: () => void;
    legacyCount: number;
    legacyLoading: boolean;
    onLegacy: () => void;
    selectedLogIds: string[];
    activeLogId?: string;
    onSelectedLogIdsChange: (ids: string[]) => void;
    onCreateSession: () => void;
    onDeleteSelected: () => void;
    onPreviewLog: (log: LogSummary) => void;
}) {
    const { t } = useTranslation();
    const allSelected = Boolean(logs.length) && selectedLogIds.length === logs.length;
    const toggleAll = () => onSelectedLogIdsChange(allSelected ? [] : logs.map((log) => log.id));

    return (
        <>
            <div className="mb-3 flex items-center justify-between gap-3">
                <div>
                    <h2 className="text-base font-semibold">{t("workbench.logs")}</h2>
                </div>
                <Tag className="m-0">{logs.length}</Tag>
            </div>
            <div className="mb-4 flex flex-wrap gap-2">
                <Button size="small" icon={<Plus className="size-3.5" />} onClick={onCreateSession}>
                    {t("workbench.new")}
                </Button>
                <Button size="small" icon={<CheckSquare className="size-3.5" />} disabled={!logs.length} onClick={toggleAll}>
                    {allSelected ? t("common.cancel") : t("workbench.selectAll")}
                </Button>
                <Button size="small" danger icon={<Trash2 className="size-3.5" />} disabled={!selectedLogIds.length} onClick={onDeleteSelected}>
                    {t("common.delete")}
                </Button>
            </div>
            <div className="space-y-3">
                {logs.map((log) => (
                    <LogCard
                        key={log.id}
                        log={log}
                        selected={selectedLogIds.includes(log.id)}
                        active={activeLogId === log.id}
                        onSelectedChange={(checked) => onSelectedLogIdsChange(checked ? [...selectedLogIds, log.id] : selectedLogIds.filter((id) => id !== log.id))}
                        onClick={() => onPreviewLog(log)}
                    />
                ))}
                {more && <Button block onClick={onMore}>显示更多</Button>}
                {!!legacyCount && <Button block loading={legacyLoading} onClick={onLegacy}>读取较早的本机记录（剩余 {legacyCount} 条）</Button>}
                {!logs.length && !legacyCount ? <div className="flex min-h-48 items-center justify-center rounded-lg border border-dashed border-stone-300 text-center text-sm text-stone-500 dark:border-stone-700">{t("workbench.noLogs")}</div> : null}
            </div>
        </>
    );
}

function LogCard({ log, selected, active, onSelectedChange, onClick }: { log: LogSummary; selected: boolean; active: boolean; onSelectedChange: (checked: boolean) => void; onClick: () => void }) {
    const { t } = useTranslation();
    const [storedThumbnail, setStoredThumbnail] = useState<string | null>(null);
    useEffect(() => {
        let current = true;
        void logThumbnails.getItem<string>(log.id).then((value) => { if (current) setStoredThumbnail(value); }).catch(() => undefined);
        return () => { current = false; };
    }, [log.id]);
    const thumbnail = log.thumbnail || storedThumbnail;

    return (
        <button
            type="button"
            className={`block w-full rounded-lg border p-2 text-left transition ${active ? "border-stone-900 bg-blue-50 dark:border-stone-100 dark:bg-blue-950/20" : "border-stone-200 bg-background hover:bg-stone-50 dark:border-stone-800 dark:hover:bg-stone-900"}`}
            onClick={onClick}
        >
            <div className="grid grid-cols-[minmax(128px,1fr)_auto] gap-2">
                <div className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-start gap-2">
                    <Checkbox className="mt-0.5" checked={selected} onClick={(event) => event.stopPropagation()} onChange={(event) => onSelectedChange(event.target.checked)} />
                    <div className="min-w-0">
                        <div className="truncate text-sm font-semibold leading-5">{log.title}</div>
                        {log.localSave && <span className="text-xs text-amber-600 dark:text-amber-400">{t(`imageWorkbench.save_${log.localSave}`)}</span>}
                        {thumbnail && <img src={thumbnail} alt="" loading="lazy" decoding="async" className="mt-2 size-8 rounded-md object-cover" />}
                    </div>
                </div>
                <div className="grid justify-items-end gap-2">
                    <div className="flex gap-1">
                        <Tag className="m-0 flex h-6 items-center rounded-md px-1.5 text-xs leading-none" color="blue">
                            {t("workbench.successCount", { count: log.successCount ?? log.imageCount })}
                        </Tag>
                        {log.failCount ? (
                            <Tag className="m-0 flex h-6 items-center rounded-md px-1.5 text-xs leading-none" color="red">
                                {t("workbench.failCount", { count: log.failCount })}
                            </Tag>
                        ) : null}
                    </div>
                    <div className="flex flex-wrap justify-end gap-1">
                        <Tag className="m-0 flex h-6 items-center rounded-md px-1.5 text-xs leading-none">{t("workbench.itemCount", { count: log.imageCount })}</Tag>
                        <Tag className="m-0 flex h-6 items-center rounded-md px-1.5 text-xs leading-none" color="green">
                            {formatDuration(log.durationMs)}
                        </Tag>
                    </div>
                    <div className="flex justify-end">
                        <Tag className="m-0 flex h-6 items-center rounded-md px-1.5 text-xs leading-none">{log.time}</Tag>
                    </div>
                </div>
            </div>
        </button>
    );
}

function newestFirst(a: LogSummary, b: LogSummary) {
    return b.createdAt - a.createdAt;
}

async function readLogIndex() {
    const saved: LogSummary[] = [];
    const [, keys] = await Promise.all([logIndex.iterate<LogSummary, void>((log) => { saved.push(log); }), logStore.keys()]);
    const indexed = new Set(saved.map((log) => log.id));
    const existing = new Set(keys);
    return { saved: saved.filter((log) => existing.has(log.id)).sort(newestFirst), legacy: keys.filter((id) => !indexed.has(id)) };
}

function summarizeLog(log: GenerationLog, id = log.id): LogSummary {
    const { references: _references, images: _images, ...summary } = log;
    return { ...summary, id, title: (log.title || log.model || i18n.t("workbench.untitled")).slice(0, 120), prompt: "", createdAt: log.createdAt || 0 };
}

async function originalImageBlob(image: GeneratedImage) {
    // WebKit in a nested page can reject fetch(data:). Decode locally like the download button.
    if (image.dataUrl.startsWith("data:")) {
        const file = dataUrlToFile({ id: image.id, name: "original", type: image.mimeType || "image/png", dataUrl: image.dataUrl });
        if (file.size) return file;
        throw new Error(i18n.t("common.imageReadFailed"));
    }
    if (image.storageKey) {
        const blob = await getImageBlob(image.storageKey);
        if (blob) return blob;
    }
    return fetchImageBlob(image.dataUrl, { signal: AbortSignal.timeout(60_000) });
}

async function historyThumbnail(image: GeneratedImage) {
    // The result is a trusted data: or local blob: URL. Draw from the loaded image
    // so thumbnail creation also works in older WebKit without createImageBitmap.
    const original = new window.Image();
    original.src = image.dataUrl;
    await original.decode();
    const { width, height } = getThumbnailDimensions(original.naturalWidth, original.naturalHeight, 96);
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    if (!context) return undefined;
    context.drawImage(original, 0, 0, width, height);
    const data = canvas.toDataURL("image/webp", 0.72);
    if (data.length <= 16 * 1024) return data;
    const compact = canvas.toDataURL("image/jpeg", 0.65);
    return compact.length <= 16 * 1024 ? compact : undefined;
}

async function normalizeLog(log: Partial<GenerationLog>): Promise<GenerationLog> {
    const references = await Promise.all(
        (log.references || []).map(async (item) => {
            void ensureImagePreview(item.storageKey);
            return { ...item, dataUrl: await resolveImageUrl(item.storageKey, item.dataUrl) };
        }),
    );
    const images = await Promise.all(
        (log.images || []).map(async (item) => {
            void ensureImagePreview(item.storageKey);
            return { ...item, dataUrl: await resolveImageUrl(item.storageKey, item.dataUrl) };
        }),
    );
    const config = normalizeLogConfig(log);
    return {
        id: log.id || nanoid(),
        createdAt: log.createdAt || Date.now(),
        title: log.title || log.model || i18n.t("workbench.untitled"),
        prompt: log.prompt || log.title || "",
        time: log.time || new Date().toLocaleString(i18n.resolvedLanguage, { hour12: false }),
        model: log.model || config.imageModel || "",
        config,
        references,
        durationMs: log.durationMs || 0,
        successCount: log.successCount ?? log.imageCount ?? 0,
        failCount: log.failCount || 0,
        imageCount: log.imageCount || log.successCount || 0,
        size: log.size || config.size || "",
        quality: log.quality || config.quality || "",
        status: log.status || "success",
        images, localSave: log.localSave,
    };
}

function serializeLog(log: GenerationLog): GenerationLog {
    return {
        ...log,
        references: log.references.map((item) => ({ ...item, dataUrl: item.storageKey ? "" : item.dataUrl })),
        images: log.images.map((image) => ({ ...image, dataUrl: image.storageKey ? "" : image.dataUrl })),
    };
}

function normalizeLogConfig(log: Partial<GenerationLog>): GenerationLogConfig {
    return {
        model: log.config?.model || log.model || "",
        imageModel: log.config?.imageModel || log.model || "",
        quality: log.config?.quality || log.quality || "",
        size: log.config?.size || log.size || "",
        count: log.config?.count || String(log.imageCount || log.successCount || 1),
    };
}

function moveListItem<T>(items: T[], index: number, offset: number) {
    const targetIndex = index + offset;
    if (targetIndex < 0 || targetIndex >= items.length) return items;
    const next = [...items];
    [next[index], next[targetIndex]] = [next[targetIndex], next[index]];
    return next;
}

function ReferenceOrderButtons({ index, total, onMove }: { index: number; total: number; onMove: (offset: number) => void }) {
    if (total <= 1) return null;
    return (
        <div className="absolute inset-x-1 bottom-1 flex justify-between">
            <Button size="small" className="!h-6 !w-6 !min-w-6 !rounded-full !bg-white/85 !p-0 !shadow-sm" icon={<ArrowLeft className="size-3" />} disabled={index <= 0} onClick={() => onMove(-1)} />
            <Button size="small" className="!h-6 !w-6 !min-w-6 !rounded-full !bg-white/85 !p-0 !shadow-sm" icon={<ArrowRight className="size-3" />} disabled={index >= total - 1} onClick={() => onMove(1)} />
        </div>
    );
}

function buildLog({
    prompt,
    model,
    config,
    references,
    durationMs,
    successCount,
    failCount,
    status,
    images,
}: {
    prompt: string;
    model: string;
    config: GenerationLogConfig;
    references: ReferenceImage[];
    durationMs: number;
    successCount: number;
    failCount: number;
    status: GenerationLog["status"];
    images: GeneratedImage[];
}): GenerationLog {
    const logConfig = {
        model: config.model,
        imageModel: config.imageModel,
        quality: config.quality,
        size: config.size,
        count: config.count,
    };
    return {
        id: nanoid(),
        createdAt: Date.now(),
        title: prompt.slice(0, 12) || i18n.t("workbench.untitled"),
        prompt,
        time: new Date().toLocaleString(i18n.resolvedLanguage, { hour12: false }),
        model,
        config: logConfig,
        references,
        durationMs,
        successCount,
        failCount,
        imageCount: successCount + failCount,
        size: logConfig.size,
        quality: logConfig.quality,
        status,
        images,
    };
}
