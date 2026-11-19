import { afterAll, expect, test } from "bun:test";
import type { AiConfig, ModelCapability } from "../src/stores/use-config-store";
import type { CanvasNodeData } from "../src/types/canvas";

// Config and i18n use browser storage during module initialization.
const values = new Map<string, string>();
const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
};
const globals = { localStorage: storage, window: { localStorage: storage } };
const original = Object.getOwnPropertyDescriptors(globalThis);
for (const [key, value] of Object.entries(globals)) Object.defineProperty(globalThis, key, { configurable: true, value });
afterAll(() => {
    for (const key of Object.keys(globals)) {
        if (original[key]) Object.defineProperty(globalThis, key, original[key]);
        else Reflect.deleteProperty(globalThis, key);
    }
});

const { CONFIG_STORE_KEY, defaultConfig, resolveModelForCapability, resolveModelRequestConfig, resolveModelScript, setLumiviaChannel, useConfigStore } = await import("../src/stores/use-config-store");
const { importAppConfig } = await import("../src/services/config-file");
const { buildGenerationConfig } = await import("../src/lib/canvas/canvas-generation-helpers");
const { CanvasNodeType } = await import("../src/types/canvas");

const config: AiConfig = {
    ...defaultConfig,
    videoModel: "replacement::video-b",
    channels: [
        {
            id: "replacement",
            name: "Replacement",
            baseUrl: "https://example.com",
            apiKey: "test-key",
            apiFormat: "openai",
            models: [
                { name: "video-b", capability: "video" },
                { name: "video-a", capability: "video" },
                { name: "image-a", capability: "image" },
            ],
        },
    ],
};
const ready = useConfigStore.getState().isAiConfigReady;
const videoNode = (model: string): CanvasNodeData => ({
    id: "saved-video",
    type: CanvasNodeType.Video,
    title: "Saved video",
    position: { x: 0, y: 0 },
    width: 320,
    height: 180,
    metadata: { model, videoTaskId: "original-task", videoTaskProvider: "openai", seconds: "4" },
});

test("a saved video on a removed provider is not replaced by the default video model", () => {
    const selected = buildGenerationConfig(config, videoNode("removed::video-a"), "video");
    expect(selected.model).toBe("removed::video-a");
    expect(ready(selected, selected.model)).toBe(false);
});

test("removing a model from an existing provider prevents retry even with valid credentials", () => {
    const selected = buildGenerationConfig(config, videoNode("replacement::deleted-video"), "video");
    expect(selected.model).toBe("replacement::deleted-video");
    expect(ready(selected, selected.model)).toBe(false);
});

test("task recovery rejects a missing provider instead of querying the first provider", () => {
    expect(ready(config, "removed::video-a")).toBe(false);
    expect(ready(config, "unknown-video")).toBe(false);
});

test("an explicitly selected unavailable default is not replaced by the built-in default", () => {
    const selected = buildGenerationConfig({ ...config, videoModel: "removed::video-a" }, undefined, "video");
    expect(selected.model).toBe("removed::video-a");
    expect(ready(selected, selected.model)).toBe(false);
});

test("a new node without a selection still uses its configured default", () => {
    const selected = buildGenerationConfig(config, undefined, "video");
    expect(selected.model).toBe("replacement::video-b");
    expect(ready(selected, selected.model)).toBe(true);
});

test("switching generation capability still selects a model of the new capability", () => {
    expect(resolveModelForCapability(config, "replacement::image-a", "video")).toBe("replacement::video-b");
});

test("changing only the key on the same provider preserves the saved model and task", () => {
    const node = videoNode("replacement::video-a");
    const selected = buildGenerationConfig({ ...config, channels: [{ ...config.channels[0], apiKey: "rotated-key" }] }, node, "video");
    expect(selected.model).toBe("replacement::video-a");
    expect(ready(selected, selected.model)).toBe(true);
    expect(node.metadata?.videoTaskId).toBe("original-task");
    expect(ready({ ...selected, channels: [{ ...config.channels[0], apiKey: "" }] }, selected.model)).toBe(false);
});

test("unavailable explicit selections are preserved for every generation capability", () => {
    for (const capability of ["image", "video", "text", "audio"] as ModelCapability[]) {
        expect(resolveModelForCapability(config, `removed::${capability}`, capability)).toBe(`removed::${capability}`);
    }
});


test("saved external providers and keys cannot participate before the main account is validated", async () => {
    values.set(CONFIG_STORE_KEY, JSON.stringify({ state: { config: { ...config, count: "2", proxyEnabled: true, apiKey: "saved-key" } }, version: 0 }));
    await useConfigStore.persist.rehydrate();
    const hydrated = useConfigStore.getState().config;
    expect(hydrated.channels).toEqual([]);
    expect(hydrated.apiKey).toBe("");
    expect(hydrated.proxyEnabled).toBe(false);
    expect(hydrated.count).toBe("2");
    expect(() => resolveModelRequestConfig(config, "replacement::image-a", "image")).toThrow();
});

test("imported credentials and scripts cannot replace the current account's image provider", async () => {
    const file = new File([JSON.stringify({
        app: "infinite-canvas",
        version: 1,
        config: {
            ...config,
            channels: [{ ...config.channels[0], id: "lumivia-image", models: [{ name: "gpt-image-2", capability: "image", script: "injected" }] }],
            imageModel: "lumivia-image::gpt-image-2",
            proxyEnabled: true,
        },
        webdav: { url: "https://example.test/dav" },
        promptSources: { sources: [], schedule: { intervalMinutes: 30, lastFetchedAt: "" } },
    })], "config.json");
    setLumiviaChannel({ group: "生图", models: ["gpt-image-2"] });
    try {
        await importAppConfig(file);
        const managed = useConfigStore.getState().config;
        expect(managed.imageModel).toBe("lumivia-image::gpt-image-2");
        expect(managed.channels).toHaveLength(1);
        expect(resolveModelScript(managed, managed.imageModel)).toBe("");
        expect(resolveModelRequestConfig(managed, managed.imageModel, "image").baseUrl).toBe("/lumivia");
        expect(() => resolveModelRequestConfig(managed, managed.imageModel, "video")).toThrow();
        expect(() => resolveModelRequestConfig(managed, "replacement::gpt-image-2", "image")).toThrow();
        expect(useConfigStore.getState().webdav.url).toBe("https://example.test/dav");
        const saved = JSON.parse(values.get(CONFIG_STORE_KEY) || "{}").state.config;
        expect(saved.channels).toEqual([]);
        expect(saved.apiKey).toBe("");
    } finally {
        setLumiviaChannel(null);
    }
    expect(() => resolveModelRequestConfig(config, "lumivia-image::gpt-image-2", "image")).toThrow();
});

test("chat selections are scoped to a key, including after a revocation or account switch", () => {
    const image = { group: "生图", models: ["gpt-image-2"] };
    const first = { id: "chat-101", name: "个人 · 标准", models: ["shared"] };
    const second = { id: "chat-102", name: "工作 · 快速", models: ["shared"] };
    try {
        setLumiviaChannel({ ...image, chatChannels: [first, second] });
        const initial = useConfigStore.getState().config;
        expect(initial.textModel).toBe("chat-101::shared");
        expect(initial.imageModel).toBe("lumivia-image::gpt-image-2");
        expect(() => resolveModelRequestConfig(initial, "shared", "text")).toThrow();
        useConfigStore.getState().updateConfig("textModel", "chat-102::shared");
        setLumiviaChannel({ ...image, chatChannels: [first, second] });
        const selected = useConfigStore.getState().config;
        expect(resolveModelRequestConfig(selected, selected.textModel, "text").channelId).toBe(second.id);
        expect(() => resolveModelRequestConfig(selected, selected.textModel, "image")).toThrow();
        setLumiviaChannel({ ...image, chatChannels: [first] });
        expect(() => resolveModelRequestConfig(selected, selected.textModel, "text")).toThrow();
        expect(useConfigStore.getState().config.channels.map((c) => c.id)).not.toContain(second.id);
    } finally { setLumiviaChannel(null); }
    expect(useConfigStore.getState().config.textModel).toBe("");
    expect(() => resolveModelRequestConfig(config, "chat-101::shared", "text")).toThrow();
});
