import { useEffect, useRef, useState } from "react";
import { ArrowUp, Bot, Plus, Square, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useLumiviaAccount } from "@/components/layout/lumivia-account";
import { ModelPicker } from "@/components/model-picker";
import { getNodeSpec } from "@/lib/canvas/node-registry";
import { generateAgentNote, requestsTextNode } from "@/services/api/lumivia-agent-note";
import { requestImageQuestion } from "@/services/api/image";
import { useAgentStore } from "@/stores/use-agent-store";
import { selectableModelsByCapability, useEffectiveConfig } from "@/stores/use-config-store";
import { CanvasNodeType } from "@/types/canvas";

type Message = { id: string; role: "user" | "assistant"; content: string };

export function LumiviaAgentChat() {
    const { t } = useTranslation();
    const { account, syncing, error: accountError } = useLumiviaAccount();
    const config = useEffectiveConfig();
    const models = selectableModelsByCapability(config, "text");
    const [choice, setChoice] = useState("");
    const model = [choice, config.textModel, ...models].find((id) => !!id && models.includes(id)) || "";
    const [messages, setMessages] = useState<Message[]>([]);
    const [partial, setPartial] = useState<string | null>(null);
    const [prompt, setPrompt] = useState("");
    const [error, setError] = useState("");
    const controller = useRef<AbortController | null>(null);
    const inserted = useRef(new Set<string>());
    const log = useRef<HTMLDivElement>(null);
    const projectId = useAgentStore((s) => s.canvasContext?.snapshot.projectId);
    const close = useAgentStore((s) => s.closePanel);
    const mountedAccount = account?.id;
    useEffect(() => () => controller.current?.abort(), []);
    useEffect(() => { if (log.current) log.current.scrollTop = log.current.scrollHeight; }, [messages, partial]);

    function addToCanvas({ id, content }: Message) {
        const ctx = useAgentStore.getState().canvasContext;
        if (inserted.current.has(id) || !ctx || ctx.snapshot.projectId !== projectId || useLumiviaAccount.getState().account?.id !== mountedAccount) return;
        const { x, y } = ctx.getCanvasCenter();
        const { width, height } = getNodeSpec(CanvasNodeType.Text);
        ctx.applyOps([{ type: "add_node", id, nodeType: CanvasNodeType.Text, title: content.split("\n", 1)[0].slice(0, 32), position: { x: x - width / 2, y: y - height / 2 }, metadata: { content, status: "success" } }]);
        inserted.current.add(id);
        setMessages((previous) => [...previous]);
    }

    async function send() {
        if (!account || !model || !prompt.trim() || controller.current) return;
        const outgoing: Message[] = [...messages, { id: crypto.randomUUID(), role: "user", content: prompt.trim() }];
        const createNote = Boolean(projectId) && requestsTextNode(prompt);
        const history = outgoing.map(({ role, content }) => ({ role, content }));
        const abort = new AbortController();
        controller.current = abort;
        setMessages(outgoing); setPrompt(""); setError(""); setPartial("");
        const current = () => !abort.signal.aborted && useLumiviaAccount.getState().account?.id === mountedAccount;
        try {
            const answer = createNote
                ? await generateAgentNote(config, model, history, abort.signal)
                : await requestImageQuestion({ ...config, model }, history, (text) => { if (current()) setPartial(text); }, { signal: abort.signal });
            if (!current()) return;
            const reply: Message = { id: crypto.randomUUID(), role: "assistant", content: answer };
            if (createNote) addToCanvas(reply);
            setMessages([...outgoing, reply]);
        } catch (e) {
            if (current()) setError(e instanceof Error ? e.message : t("apiErrors.requestFailed"));
        } finally {
            if (controller.current === abort) { controller.current = null; setPartial(null); }
        }
    }
    const unavailable = accountError || (!account && syncing ? t("config.lumivia.checking") : !account ? t("config.lumivia.accountRequired") : !model ? t("agent.hosted.noModels") : "");
    return <section className="flex h-full min-h-0 flex-col" aria-label={t("agent.hosted.title")}>
        <header className="flex h-14 shrink-0 items-center gap-2 border-b border-border/60 px-4"><Bot className="size-5" /><h2 className="flex-1 font-medium">{t("agent.hosted.title")}</h2><button onClick={close} aria-label={t("topNav.closeAgent")} className="rounded p-1 hover:bg-muted"><X className="size-4" /></button></header>
        <div ref={log} role="log" className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4 text-sm">
            {!messages.length && <p className="text-muted-foreground">{unavailable || t("agent.hosted.empty")}</p>}
            {messages.map((item) => <div key={item.id} className={item.role === "user" ? "ml-8 rounded-2xl bg-muted px-3 py-2" : "mr-3 py-1"}>
                <p className="whitespace-pre-wrap break-words">{item.content}</p>
                {item.role === "assistant" && projectId && <button disabled={inserted.current.has(item.id)} onClick={() => addToCanvas(item)} className="mt-2 inline-flex items-center gap-1 rounded border border-border px-2 py-1 text-xs hover:bg-muted"><Plus className="size-3" />{t(inserted.current.has(item.id) ? "agent.hosted.inserted" : "agent.hosted.insert")}</button>}
            </div>)}
            {partial !== null && <p className="whitespace-pre-wrap break-words">{partial || t("agent.hosted.waiting")}</p>}
            {error && <p role="alert" className="text-destructive">{error}</p>}
        </div>
        <form onSubmit={(e) => { e.preventDefault(); void send(); }} className="space-y-2 border-t border-border/60 p-3">
            <ModelPicker config={config} value={model} capability="text" onChange={setChoice} />
            <div className="flex items-end gap-2 rounded-xl border border-border p-2">
                <textarea rows={2} value={prompt} onChange={(e) => setPrompt(e.target.value)} aria-label={t("agent.hosted.prompt")} placeholder={t("agent.hosted.prompt")} disabled={!model || !account} className="min-w-0 flex-1 resize-none bg-transparent text-sm outline-none" onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); } }} />
                {partial !== null ? <button type="button" onClick={() => controller.current?.abort()} aria-label={t("agent.hosted.stop")} className="rounded-full bg-foreground p-2 text-background"><Square className="size-4" /></button> : <button type="submit" disabled={!model || !account || !prompt.trim()} aria-label={t("agent.hosted.send")} className="rounded-full bg-foreground p-2 text-background disabled:opacity-35"><ArrowUp className="size-4" /></button>}
            </div>
        </form>
    </section>;
}
