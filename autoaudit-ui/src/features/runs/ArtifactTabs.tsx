import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { MarkdownView } from "@/components/MarkdownView";
import { strings } from "@/strings";
import { api } from "@/api/client";
import {
  useCreateVerificationViews,
  useExportReport,
} from "@/api/hooks";
import type { HealthStatus, RunArtifacts } from "@/api/types";
import { toast } from "sonner";

export interface ArtifactTabsProps {
  runId: string;
  artifacts: RunArtifacts;
  revitStatus: HealthStatus | "checking";
  initialTab?: string;
}

function useArtifactText(runId: string, path: string, enabled: boolean) {
  return useQuery({
    queryKey: ["artifact-text", runId, path],
    queryFn: () => api.get<string>(`/runs/${encodeURIComponent(runId)}/${path}`),
    enabled,
  });
}

export function ArtifactTabs({ runId, artifacts, revitStatus, initialTab }: ArtifactTabsProps) {
  const [tab, setTab] = useState(initialTab ?? "report");
  const [traceOpen, setTraceOpen] = useState(false);
  const [confirmExport, setConfirmExport] = useState<"docx" | "pdf" | null>(null);
  const [confirmViews, setConfirmViews] = useState(false);

  const report = useArtifactText(runId, "report", tab === "report" && artifacts.report);
  const verification = useArtifactText(
    runId,
    "verification-report",
    tab === "verification" && artifacts.verification_report,
  );
  const trace = useArtifactText(runId, "artifacts/trace.md", traceOpen && artifacts.trace);

  const exportReport = useExportReport(runId);
  const createViews = useCreateVerificationViews(runId);

  const revitReady = revitStatus === "up";

  function handleExport(format: "docx" | "pdf") {
    exportReport.mutate(format, {
      onSuccess: (res) => toast.success(`${res.artifact} created`),
      onError: (err) => toast.error(String(err)),
    });
    setConfirmExport(null);
  }

  function handleCreateViews() {
    createViews.mutate(false, {
      onSuccess: (res) => toast.success(`Created ${res.created.length} view(s)`),
      onError: (err) => toast.error(String(err)),
    });
    setConfirmViews(false);
  }

  return (
    <div className="flex flex-col gap-3">
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="report">{strings.runDetail.tabReport}</TabsTrigger>
          <TabsTrigger value="verification">{strings.runDetail.tabVerificationReport}</TabsTrigger>
          <TabsTrigger value="trace">{strings.runDetail.tabTrace}</TabsTrigger>
        </TabsList>

        <TabsContent value="report">
          {artifacts.report ? (
            report.data && <MarkdownView markdown={report.data} />
          ) : (
            <p className="text-[var(--ink-muted)]">{strings.runDetail.artifactMissing("report")}</p>
          )}
        </TabsContent>

        <TabsContent value="verification">
          {artifacts.verification_report ? (
            verification.data && <MarkdownView markdown={verification.data} />
          ) : (
            <p className="text-[var(--ink-muted)]">
              {strings.runDetail.artifactMissing("verification report")}
            </p>
          )}
        </TabsContent>

        <TabsContent value="trace">
          {artifacts.trace ? (
            <Collapsible open={traceOpen} onOpenChange={setTraceOpen}>
              <CollapsibleTrigger asChild>
                <Button variant="outline" size="sm">
                  {traceOpen ? strings.common.close : strings.runDetail.tabTrace}
                </Button>
              </CollapsibleTrigger>
              <CollapsibleContent>
                <pre className="mt-2 max-h-[40vh] overflow-auto rounded-[var(--radius)] bg-[var(--surface-2)] p-2 font-mono-val text-[12px]">
                  {trace.data ?? strings.common.loading}
                </pre>
              </CollapsibleContent>
            </Collapsible>
          ) : (
            <p className="text-[var(--ink-muted)]">{strings.runDetail.artifactMissing("trace")}</p>
          )}
        </TabsContent>
      </Tabs>

      <div className="flex flex-wrap gap-2 border-t border-[var(--border)] pt-3">
        <Button variant="outline" size="sm" onClick={() => setConfirmExport("docx")}>
          {strings.runDetail.exportDocx}
        </Button>
        <Button variant="outline" size="sm" onClick={() => setConfirmExport("pdf")}>
          {strings.runDetail.exportPdf}
        </Button>
        {revitReady ? (
          <Button variant="outline" size="sm" onClick={() => setConfirmViews(true)}>
            {strings.runDetail.createViews}
          </Button>
        ) : (
          <Button variant="outline" size="sm" disabled title={strings.runDetail.highlightDisabledTooltip}>
            {strings.runDetail.createViews}
          </Button>
        )}
      </div>

      <ConfirmDialog
        open={confirmExport !== null}
        onOpenChange={(o) => !o && setConfirmExport(null)}
        title={strings.runDetail.confirmExportTitle}
        description={strings.runDetail.confirmExportBody}
        loading={exportReport.isPending}
        onConfirm={() => confirmExport && handleExport(confirmExport)}
      />
      <ConfirmDialog
        open={confirmViews}
        onOpenChange={setConfirmViews}
        title={strings.runDetail.confirmViewsTitle}
        description={strings.runDetail.confirmViewsBody}
        loading={createViews.isPending}
        onConfirm={handleCreateViews}
      />
    </div>
  );
}
