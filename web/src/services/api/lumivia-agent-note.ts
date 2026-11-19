import i18n from "@/i18n";
import type { AiConfig } from "@/stores/use-config-store";
import type { AiTextMessage } from "./image";
import { requestLumiviaChat } from "./lumivia-chat";

// Only an explicit command from the current user can create a node. Text returned by a model cannot invoke tools.
export function requestsTextNode(prompt: string) {
    const command = prompt.trim();
    if (/[?？吗呢]\s*$/.test(command)) return false;
    return /^(?:请你?|麻烦你?)?(?:帮我|给我)?(?:直接)?(?:在(?:当前|这个)?(?:画布|画板)[中上里]?)?(?:创建|新建|添加|生成|新增)(?:一[个张条]|[个张条])?(?:文字节点|文本节点|(?:文字)?便签)(?=$|[\s，。,:：、！!]|内容|用来|用于|写上)/u.test(command)
        || /^(?:please\s+)?(?:create|add|make)\s+(?:a\s+)?(?:text node|sticky note)\b/i.test(command);
}

export async function generateAgentNote(config: AiConfig, model: string, messages: AiTextMessage[], signal: AbortSignal) {
    const response = await requestLumiviaChat(config, model, {
        messages: [{ role: "system", content: "Write only the text to put inside the requested canvas note, in the user's language. The canvas will create the node." }, ...messages],
        stream: false,
    }, signal);
    const payload = await response.json();
    if (!response.ok || payload?.error) throw new Error(payload?.error?.message || i18n.t("apiErrors.requestFailed"));
    const choice = payload?.choices?.[0];
    const content = choice?.message?.content;
    if (choice?.finish_reason !== "stop" || choice?.message?.refusal || typeof content !== "string" || !content.trim()) throw new Error(i18n.t("agent.hosted.noteIncomplete"));
    return content.trim();
}
