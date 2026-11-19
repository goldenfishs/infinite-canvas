import { useEffect, useRef } from "react";
import { create } from "zustand";
import i18n from "@/i18n";
import { setLumiviaChannel, type LumiviaChatChannel } from "@/stores/use-config-store";

const MAIN_ORIGIN = "https://api.lumivia.cn";
const text = (key: string) => i18n.t(`config.lumivia.${key}`);

type Account = { id: number; email: string; isAdmin?: boolean; group: string; prices: Record<string, number>; models: string[]; imageWarning?: string; chatChannels?: LumiviaChatChannel[] };
type ImageAction = "generations" | "edits";
type GrantTarget = { action: ImageAction | "storage" | "storage-admin" } | { action: "chat"; channelId: string; model: string };
type Request = { action: "account" } | (GrantTarget & { challenge: string; accountId: number });
type Reply = { status: number; account?: Account; grant?: string; error?: string };
type Pending = { send: () => void; finish: (value: Reply) => void };

let helper: Window | null = null;
let ready = false;
let requestId = 0;
let syncVersion = 0;
let accountVersion = 0;
const pending = new Map<number, Pending>();

function authorize(request: Request): Promise<Reply> {
    return new Promise((resolve) => {
        const id = ++requestId;
        const timer = window.setTimeout(() => finish({ status: 503 }), 20000);
        function finish(reply: Reply) {
            window.clearTimeout(timer);
            pending.delete(id);
            resolve(reply);
        }
        const send = () => helper?.postMessage({ type: "lumivia:authorize", id, ...request }, MAIN_ORIGIN);
        pending.set(id, { send, finish });
        if (ready) send();
    });
}

export const useLumiviaAccount = create<{
    account: Account | null;
    error: string;
    syncing: boolean;
    sync: () => Promise<void>;
}>(() => ({ account: null, error: "", syncing: true, sync: syncLumiviaAccount }));

function applyAccount(account: Account | null, error = "", syncing = false) {
    if (account?.id !== useLumiviaAccount.getState().account?.id) accountVersion++;
    setLumiviaChannel(account);
    useLumiviaAccount.setState({ account, error, syncing });
}

function replyError(reply: Reply) {
    if (reply.status === 401) return text("accountRequired");
    if (reply.status === 409) return text("accountChanged");
    return reply.error || text("syncFailed");
}

export async function syncLumiviaAccount() {
    const version = ++syncVersion;
    useLumiviaAccount.setState({ syncing: true, error: "" });
    const reply = await authorize({ action: "account" });
    if (version !== syncVersion) return;
    if (reply.status === 200 && reply.account) applyAccount(reply.account);
    else applyAccount(null, replyError(reply));
}

export function lumiviaImageAccount() {
    const account = useLumiviaAccount.getState().account;
    if (!account) throw new Error(text("accountRequired"));
    return { id: account.id, version: accountVersion };
}

export function lumiviaImageHeaders(action: ImageAction, account: ReturnType<typeof lumiviaImageAccount>, signal?: AbortSignal) {
    return lumiviaGrantHeaders({ action }, account, signal);
}

export function lumiviaChatHeaders(channelId: string, model: string, account: ReturnType<typeof lumiviaImageAccount>, signal?: AbortSignal) {
    return lumiviaGrantHeaders({ action: "chat", channelId, model }, account, signal);
}

export function lumiviaStorageHeaders(admin: boolean, account: ReturnType<typeof lumiviaImageAccount>, signal?: AbortSignal) {
    return lumiviaGrantHeaders({ action: admin ? "storage-admin" : "storage" }, account, signal);
}

async function lumiviaGrantHeaders(target: GrantTarget, account: ReturnType<typeof lumiviaImageAccount>, signal?: AbortSignal) {
    const hex = (bytes: Uint8Array) => Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    signal?.throwIfAborted();
    if (account.version !== accountVersion) throw new Error(text("accountChanged"));
    const verifier = hex(crypto.getRandomValues(new Uint8Array(32)));
    const challenge = hex(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
    const reply = await authorize({ ...target, challenge, accountId: account.id });
    signal?.throwIfAborted();
    if (account.version !== accountVersion) throw new Error(text("accountChanged"));
    if (reply.status !== 200 || !reply.grant) {
        if ([401, 403, 409].includes(reply.status)) {
            applyAccount(null, replyError(reply), true);
            void syncLumiviaAccount();
        }
        throw new Error(replyError(reply));
    }
    return { "X-Lumivia-Grant": reply.grant, "X-Lumivia-Verifier": verifier };
}

export function LumiviaAccount() {
    const iframe = useRef<HTMLIFrameElement>(null);
    useEffect(() => {
        const frame = iframe.current;
        if (!frame) return;
        helper = frame.contentWindow;
        const onMessage = (event: MessageEvent) => {
            if (event.origin !== MAIN_ORIGIN || event.source !== helper) return;
            const data = event.data;
            if (data?.type === "lumivia:ready") {
                ready = true;
                pending.forEach((request) => request.send());
            } else if (data?.type === "lumivia:authorization") {
                pending.get(data.id)?.finish(data);
            } else if (data?.type === "lumivia:account-changed") {
                accountVersion++;
                pending.forEach((request) => request.finish({ status: 409 }));
                applyAccount(null, "", true);
                void syncLumiviaAccount();
            }
        };
        const onVisible = () => { if (document.visibilityState === "visible") void syncLumiviaAccount(); };
        window.addEventListener("message", onMessage);
        window.addEventListener("focus", syncLumiviaAccount);
        document.addEventListener("visibilitychange", onVisible);
        frame.src = `${MAIN_ORIGIN}/canvas-connect/`;
        void syncLumiviaAccount();
        return () => {
            window.removeEventListener("message", onMessage);
            window.removeEventListener("focus", syncLumiviaAccount);
            document.removeEventListener("visibilitychange", onVisible);
            helper = null;
            ready = false;
            syncVersion++;
            accountVersion++;
            pending.forEach((request) => request.finish({ status: 503 }));
            applyAccount(null);
        };
    }, []);
    return <iframe ref={iframe} title="乐米账号同步" referrerPolicy="no-referrer" hidden />;
}
