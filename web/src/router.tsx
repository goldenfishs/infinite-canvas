import { lazy, Suspense } from "react";
import { createBrowserRouter, Outlet } from "react-router-dom";

import { AnalyticsTracker } from "@/components/layout/analytics-tracker";
import UserLayout from "@/layouts/user-layout";
import NotFound from "@/pages/not-found";

const HomePage = lazy(() => import("@/pages/home"));
const AssetsPage = lazy(() => import("@/pages/assets"));
const CanvasPage = lazy(() => import("@/pages/canvas"));
const CanvasProjectPage = lazy(() => import("@/pages/canvas/project"));
const ConfigPage = lazy(() => import("@/pages/config"));
const ImagePage = lazy(() => import("@/pages/image"));
const PromptsPage = lazy(() => import("@/pages/prompts"));
const StoragePage = lazy(() => import("@/pages/storage"));
const JobsPage = lazy(() => import("@/pages/jobs"));
const OperationsPage = lazy(() => import("@/pages/operations"));
const LocalVideoPage = lazy(() => import("@/pages/video"));

export const router = createBrowserRouter([
    {
        element: (
            <UserLayout>
                <AnalyticsTracker />
                <Suspense fallback={<div className="p-6 text-sm text-muted-foreground" role="status">正在打开…</div>}><Outlet /></Suspense>
            </UserLayout>
        ),
        children: [
            { path: "/", element: <HomePage /> },
            { path: "/image", element: <ImagePage /> },
            { path: "/video", element: <JobsPage /> },
            { path: "/video/local", element: <LocalVideoPage /> },
            { path: "/jobs", element: <JobsPage /> },
            { path: "/admin/canvas", element: <OperationsPage /> },
            { path: "/assets", element: <AssetsPage /> },
            { path: "/storage", element: <StoragePage /> },
            { path: "/prompts", element: <PromptsPage /> },
            { path: "/canvas", element: <CanvasPage /> },
            { path: "/canvas/:id", element: <CanvasProjectPage /> },
            { path: "/config", element: <ConfigPage /> },
        ],
    },
    { path: "*", element: <NotFound /> },
]);
