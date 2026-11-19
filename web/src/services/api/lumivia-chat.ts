import { lumiviaChatHeaders, lumiviaImageAccount } from "@/components/layout/lumivia-account";
import { resolveModelRequestConfig, type AiConfig } from "@/stores/use-config-store";

/** Issue a single chat call using the selected account channel, including Agent tool turns. */
export async function requestLumiviaChat(config: AiConfig, selectedModel: string, body: Record<string, unknown>, signal?: AbortSignal, account = lumiviaImageAccount()) {
    const { channelId, model } = resolveModelRequestConfig(config, selectedModel, "text");
    const headers = await lumiviaChatHeaders(channelId, model, account, signal);
    return fetch("/lumivia/v1/chat/completions", {
        method: "POST",
        headers: { ...headers, "Content-Type": "application/json", Accept: body.stream ? "text/event-stream" : "application/json" },
        body: JSON.stringify({ ...body, model }),
        signal,
    });
}
