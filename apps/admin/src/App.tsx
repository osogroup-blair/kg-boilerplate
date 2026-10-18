import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth/AuthContext";
import { Layout } from "./components/Layout";
import { Loading } from "./components/states";
import { AgentsAccess } from "./pages/AgentsAccess";
import { CommandsPage } from "./pages/commands/CommandsPage";
import { ContextLabPage } from "./pages/context/ContextLabPage";
import { ContextTracePage } from "./pages/context/ContextTracePage";
import { Dashboard } from "./pages/Dashboard";
import { DeletionDetailPage } from "./pages/deletions/DeletionDetailPage";
import { DeletionsPage } from "./pages/deletions/DeletionsPage";
import { PrivacyAuditPage } from "./pages/governance/PrivacyAuditPage";
import { SettingsPage } from "./pages/governance/SettingsPage";
import { InteractionDetailPage } from "./pages/interactions/InteractionDetailPage";
import { InteractionsPage } from "./pages/interactions/InteractionsPage";
import { DirectoryPage } from "./pages/entities/DirectoryPage";
import { EntityDetailPage } from "./pages/entities/EntityDetailPage";
import { JobDetailPage } from "./pages/operations/JobDetailPage";
import { OperationsPage } from "./pages/operations/OperationsPage";
import { OverviewTab } from "./pages/ontology/OverviewTab";
import { OntologyPage } from "./pages/ontology/OntologyPage";
import { RelationshipsTab } from "./pages/ontology/RelationshipsTab";
import { TypesTab } from "./pages/ontology/TypesTab";
import { VersionLayout } from "./pages/ontology/VersionLayout";
import { VersionsPage } from "./pages/ontology/VersionsPage";
import { YamlTab } from "./pages/ontology/YamlTab";
import { Login, OidcCallback } from "./pages/Login";
import { GraphExplorerPage } from "./pages/graph/GraphExplorerPage";
import { RebuildPage } from "./pages/graph/RebuildPage";
import { MemoriesPage } from "./pages/memories/MemoriesPage";
import { MemoryDetailPage } from "./pages/memories/MemoryDetailPage";
import { ReviewDetailPage } from "./pages/reviews/ReviewDetailPage";
import { ReviewQueuePage } from "./pages/reviews/ReviewQueuePage";

export function App() {
  const { status } = useAuth();
  if (status === "loading") return <div className="p-8"><Loading label="Restoring session…" /></div>;
  if (status === "anonymous")
    return (
      <Routes>
        <Route path="login/callback" element={<OidcCallback />} />
        <Route path="*" element={<Login />} />
      </Routes>
    );
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="agents" element={<AgentsAccess />} />
        <Route path="agents/:principalId" element={<AgentsAccess />} />
        <Route path="ontology" element={<OntologyPage />}>
          <Route index element={<VersionsPage />} />
          <Route path="versions/:versionId" element={<VersionLayout />}>
            <Route index element={<OverviewTab />} />
            <Route path="types" element={<TypesTab />} />
            <Route path="types/:typeKey" element={<TypesTab />} />
            <Route path="relationships" element={<RelationshipsTab />} />
            <Route path="relationships/:relKey" element={<RelationshipsTab />} />
            <Route path="yaml" element={<YamlTab />} />
          </Route>
        </Route>
        <Route path="entities" element={<DirectoryPage />} />
        <Route path="entities/:entityId" element={<EntityDetailPage />} />
        <Route path="graph" element={<GraphExplorerPage />} />
        <Route path="graph/rebuild" element={<RebuildPage />} />
        <Route path="memories" element={<MemoriesPage />} />
        <Route path="memories/:memoryId" element={<MemoryDetailPage />} />
        <Route path="context" element={<ContextLabPage />} />
        <Route path="context/:contextId" element={<ContextTracePage />} />
        <Route path="reviews" element={<ReviewQueuePage />} />
        <Route path="reviews/:reviewId" element={<ReviewDetailPage />} />
        <Route path="commands" element={<CommandsPage />} />
        <Route path="commands/:commandId" element={<CommandsPage />} />
        <Route path="interactions" element={<InteractionsPage />} />
        <Route path="interactions/:eventId" element={<InteractionDetailPage />} />
        <Route path="deletions" element={<DeletionsPage />} />
        <Route path="deletions/:requestId" element={<DeletionDetailPage />} />
        <Route path="privacy" element={<PrivacyAuditPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="operations" element={<OperationsPage />} />
        <Route path="operations/jobs/:jobId" element={<JobDetailPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
