import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { ErrorText, Loading } from "./components/ui";
import { AppPage } from "./pages/AppPage";
import { AppSetupPage } from "./pages/AppSetupPage";
import { AutomationPage, AutomationsPage } from "./pages/AutomationsPage";
import { BuildPage } from "./pages/BuildPage";
import { BuildSessionPage } from "./pages/BuildSessionPage";
import { BuildSetupPage } from "./pages/BuildSetupPage";
import { CapturePage } from "./pages/CapturePage";
import { CatalogPage } from "./pages/CatalogPage";
import { ComparePage } from "./pages/ComparePage";
import { HistoryPage } from "./pages/HistoryPage";
import { HomePage } from "./pages/HomePage";
import { LoginPage } from "./pages/LoginPage";
import { MachinesPage } from "./pages/MachinesPage";
import { MergePage } from "./pages/MergePage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { PackagePage } from "./pages/PackagePage";
import { PackagePreviewPage } from "./pages/PackagePreviewPage";
import { RecordingPage } from "./pages/RecordingPage";
import { RecordingsPage } from "./pages/RecordingsPage";
import { ReleasePage } from "./pages/ReleasePage";
import { SetupPage } from "./pages/SetupPage";
import { TriagePage } from "./pages/TriagePage";
import { UsersPage } from "./pages/UsersPage";
import { SessionProvider, useSessionQuery } from "./session";

export function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
}

/** The routes keep the portal's page paths, so bookmarks and journey links still work. */
export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<HomePage />} />
      <Route path="/apps" element={<CatalogPage />} />
      <Route path="/triage" element={<TriagePage />} />
      <Route path="/recordings/:recordingId/triage" element={<TriagePage />} />
      <Route path="/login" element={<Navigate to="/" replace />} />
      <Route path="/apps/:appId" element={<AppPage />} />
      <Route path="/apps/:appId/setup" element={<AppSetupPage />} />
      <Route path="/apps/:appId/history" element={<HistoryPage />} />
      <Route path="/apps/:appId/merge" element={<MergePage />} />
      <Route path="/apps/:appId/build-pack/preview" element={<PackagePreviewPage />} />
      <Route path="/apps/:appId/packages/:rev" element={<PackagePage />} />
      <Route path="/apps/:appId/build" element={<BuildPage />} />
      <Route path="/apps/:appId/sessions/compare" element={<ComparePage />} />
      <Route path="/builds/:buildId" element={<BuildSessionPage />} />
      <Route path="/releases/:releaseId" element={<ReleasePage />} />
      <Route path="/recordings" element={<RecordingsPage />} />
      <Route path="/recordings/:recordingId" element={<RecordingPage />} />
      <Route path="/sessions" element={<CapturePage />} />
      <Route path="/machines" element={<MachinesPage />} />
      <Route path="/automations" element={<AutomationsPage />} />
      <Route path="/automations/:automationId" element={<AutomationPage />} />
      <Route path="/setup" element={<SetupPage />} />
      <Route path="/setup/builds" element={<BuildSetupPage />} />
      <Route path="/admin/users" element={<UsersPage />} />
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}

/** Signed in: the pages; signed out: the sign-in form wherever the address points. */
export function Root() {
  const session = useSessionQuery();
  if (session.isPending) return <main><Loading what="Signing in" /></main>;
  if (session.isError) return <main><h1>TwinLab2</h1><ErrorText error={session.error} /></main>;
  if (!session.data.signed_in) {
    if (session.data.login_available) return <LoginPage />;
    return (
      <main>
        <h1>TwinLab2</h1>
        <p>You are not signed in. Open the portal through your organisation's sign-in.</p>
      </main>
    );
  }
  return (
    <SessionProvider session={session.data}>
      <Layout>
        <AppRoutes />
      </Layout>
    </SessionProvider>
  );
}

export function App({ client = makeQueryClient() }: { client?: QueryClient }) {
  return (
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <Root />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
