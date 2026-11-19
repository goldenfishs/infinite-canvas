import { lumiviaImageAccount, lumiviaStorageHeaders } from "@/components/layout/lumivia-account";

export const GIB = 1024 ** 3;
export const MIB = 1024 ** 2;
export type CloudFile = { id: string; mime: string; bytes: number; created: number; expires?: number | null; location?: "local" | "cos"; thumbUrl?: string | null };
export type CloudFiles = { items: CloudFile[]; more: boolean; used: number; quota: number; reserved?: number; retentionDays?: number; imageMiB?: number; videoMiB?: number };
export type CanvasUser = { id: number; email: string; quota: number | null; used: number };
export type CloudAdmin = { items: CanvasUser[]; more: boolean; defaultQuota: number; totalUsed: number; totalLimit: number; diskFree: number; reservedFree: number };
export type CanvasPolicy = {
    backend: "local" | "cos"; retentionDays: number; totalLimitMiB: number; diskReserveMiB: number;
    imageMiB: number; videoMiB: number; workers: number; perUser: number; queueSize: number; memoryGuardMiB: number;
    imageEnabled: boolean; imageGroup: number; imageMode: "auto" | "sync" | "native"; imageModels: string[];
    chatEnabled: boolean; chatGroups: number[]; chatModels: string[];
    videoEnabled: boolean; videoProtocol: "seedance" | "openai"; videoGroup: number; videoModels: string[]; videoSeconds: number;
    extraVideoRoutes: { model: string; group: number; protocol: "seedance" | "openai" }[];
    resultHosts: string[];
};
export type CosSettings = { bucket: string; region: string; domain: string; secretId?: string; secretKey?: string };
export type CanvasSettings = { policy: CanvasPolicy; revision: number; cos: (CosSettings & { checked: number }) | null; totalUsed: number; reserved: number; diskFree: number; cleanupPending: number };

export async function canvasRequest(path: string, headers: Record<string, string>, data: Record<string, unknown> | Blob | FormData, signal?: AbortSignal, account = lumiviaImageAccount()) {
    const response = await fetch(`/lumivia/${path}`, {
        method: "POST", signal, headers: {
            ...headers,
            ...(data instanceof Blob ? { "Content-Type": data.type, "X-Canvas-Bytes": String(data.size) }
                : data instanceof FormData ? {} : { "Content-Type": "application/json" }),
        },
        body: data instanceof Blob || data instanceof FormData ? data : JSON.stringify(data),
    });
    if (!response.ok) throw new Error((await response.json().catch(() => null))?.error?.message || `画布请求失败（${response.status}）`);
    if (account.version !== lumiviaImageAccount().version) throw new Error("主站账号已切换");
    return response;
}

export async function privateRequest(path: string, data: Record<string, unknown> | Blob, signal?: AbortSignal, account = lumiviaImageAccount()) {
    const grant = await lumiviaStorageHeaders(path.split("/")[1].startsWith("admin-"), account, signal);
    return canvasRequest(path, grant, data, signal, account);
}

const request = (action: string, data: Record<string, unknown> | Blob, signal?: AbortSignal, account?: ReturnType<typeof lumiviaImageAccount>) => privateRequest(`storage/${action}`, data, signal, account);

export const cloudStorage = {
    list: async (page: number, signal: AbortSignal): Promise<CloudFiles> => (await request("list", { page }, signal)).json(),
    upload: async (blob: Blob, account: ReturnType<typeof lumiviaImageAccount>) => request("upload", blob, AbortSignal.timeout(125_000), account),
    access: async (id: string, preview: boolean, signal: AbortSignal): Promise<{ url: string; previewQuality?: "thumbnail" }> => (await request("access", { id, preview }, signal)).json(),
    material: async (id: string, signal: AbortSignal, account?: ReturnType<typeof lumiviaImageAccount>) => (await request("material", { id }, signal, account)).blob(),
    delete: async (id: string, signal: AbortSignal) => request("delete", { id }, signal),
    adminList: async (page: number, search: string, signal: AbortSignal): Promise<CloudAdmin> => (await request("admin-list", { page, search }, signal)).json(),
    adminDefault: async (quota: number, signal: AbortSignal) => request("admin-default", { quota }, signal),
    adminUser: async (userId: number, quota: number | null, signal: AbortSignal) => request("admin-user", { userId, quota }, signal),
    settings: async (signal: AbortSignal): Promise<CanvasSettings> => (await request("admin-settings", {}, signal)).json(),
    saveSettings: async (revision: number, changes: CanvasPolicy, cos?: CosSettings) => request("admin-save", { revision, changes, ...(cos ? { cos } : {}) }),
};
