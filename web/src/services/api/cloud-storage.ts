import { lumiviaImageAccount, lumiviaStorageHeaders } from "@/components/layout/lumivia-account";

export const GIB = 1024 ** 3;
export type CloudFile = { id: string; mime: string; bytes: number; created: number };
export type CloudFiles = { items: CloudFile[]; more: boolean; used: number; quota: number };
export type CanvasUser = { id: number; email: string; quota: number | null; used: number };
export type CloudAdmin = { items: CanvasUser[]; more: boolean; defaultQuota: number; totalUsed: number; totalLimit: number; diskFree: number; reservedFree: number };

type Action = "list" | "upload" | "read" | "delete" | "admin-list" | "admin-default" | "admin-user";

async function request(action: Action, data: Record<string, unknown> | Blob, signal?: AbortSignal, account = lumiviaImageAccount()) {
    const grant = await lumiviaStorageHeaders(action.startsWith("admin-"), account, signal);
    const upload = data instanceof Blob;
    const response = await fetch(`/lumivia/storage/${action}`, {
        method: "POST",
        signal,
        headers: { ...grant, ...(upload ? { "Content-Type": data.type, "X-Canvas-Bytes": String(data.size) } : { "Content-Type": "application/json" }) },
        body: upload ? data : JSON.stringify(data),
    });
    if (!response.ok) throw new Error((await response.json().catch(() => null))?.error?.message || `云图库请求失败（${response.status}）`);
    if (account.version !== lumiviaImageAccount().version) throw new Error("主站账号已切换");
    return response;
}

export const cloudStorage = {
    list: async (page: number, signal: AbortSignal): Promise<CloudFiles> => (await request("list", { page }, signal)).json(),
    upload: async (blob: Blob, account: ReturnType<typeof lumiviaImageAccount>) => request("upload", blob, AbortSignal.timeout(125_000), account),
    read: async (id: string, signal: AbortSignal) => (await request("read", { id }, signal)).blob(),
    delete: async (id: string, signal: AbortSignal) => request("delete", { id }, signal),
    adminList: async (page: number, search: string, signal: AbortSignal): Promise<CloudAdmin> => (await request("admin-list", { page, search }, signal)).json(),
    adminDefault: async (quota: number, signal: AbortSignal) => request("admin-default", { quota }, signal),
    adminUser: async (userId: number, quota: number | null, signal: AbortSignal) => request("admin-user", { userId, quota }, signal),
};
