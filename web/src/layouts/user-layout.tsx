import { lazy, Suspense, type ReactNode } from "react";

import { useAgentStore } from "@/stores/use-agent-store";
import { AppTopNav } from "@/components/layout/app-top-nav";
import { LumiviaAccount } from "@/components/layout/lumivia-account";

const AgentPanel = lazy(() => import("@/components/agent/agent-panel").then((m) => ({ default: m.AgentPanel })));

export default function UserLayout({ children }: { children: ReactNode }) {
    const agentMounted = useAgentStore((state) => state.panelMounted);
    return (
        <div className="flex h-dvh overflow-hidden bg-background text-foreground">
            <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
                <LumiviaAccount />
                <AppTopNav />
                <div className="min-h-0 flex-1 overflow-hidden">{children}</div>
            </div>
            {agentMounted && <Suspense fallback={null}><AgentPanel /></Suspense>}
        </div>
    );
}
