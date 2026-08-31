import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { ChevronDown, ChevronRight, Download, FileText, FileWarning, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ApiErrorBanner } from "@/components/ApiErrorBanner";
import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { MonoText } from "@/components/MonoText";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { strings } from "@/strings";
import { basename } from "@/lib/path";
import { formatDateTime } from "@/lib/format";
import { api } from "@/api/client";
import { useDeleteRuleset, useRuleset, useRulesFiles } from "@/api/hooks";
import { ExtractPdfDialog } from "./ExtractPdfDialog";
import type { RuleDict, RuleFileDetailResponse, RuleFileSummary } from "@/api/types";

const REQUIREMENT_LABELS: Record<string, string> = {
  present_and_nonempty: "Has a value",
  canonical_format: "Canonical format",
  numeric_compare: "Numeric comparison",
  matches_regex: "Match pattern",
  not_matches_regex: "Match pattern (negated)",
  matches_regex_if_present: "Match pattern (if present)",
  unique_in_set: "Unique",
  relation_compare: "Related element",
  positive_number: "Positive number (legacy)",
  numeric_min: "Numeric minimum (legacy)",
  numeric_min_conditional: "Numeric minimum, filtered (legacy)",
  fire_rating_ge: "Fire rating vs related (legacy)",
  value_in_subset: "In allowed set",
};

function ruleActionLabel(rule: RuleDict): string {
  return rule.fixability === "auto" ? strings.rules.actionFix : strings.rules.actionIssue;
}

function RuleRow({ file, rule }: { file: string; rule: RuleDict }) {
  const navigate = useNavigate();
  return (
    <tr
      className="h-[30px] cursor-pointer border-t border-[var(--border)] hover:bg-[var(--surface-2)]"
      onClick={() => navigate(`/rule-builder?file=${encodeURIComponent(file)}&rule=${encodeURIComponent(rule.id)}`)}
    >
      <td className="px-2">
        <MonoText>{rule.id}</MonoText>
      </td>
      <td className="px-2">{rule.category ?? "—"}</td>
      <td className="px-2 font-mono-val">{rule.parameter}</td>
      <td className="px-2">{REQUIREMENT_LABELS[rule.requirement] ?? rule.requirement}</td>
      <td className="px-2">
        <Badge variant="outline" color={rule.severity_level === "severity_high" ? "var(--sev-high)" : rule.severity_level === "severity_low" ? "var(--sev-low)" : "var(--sev-medium)"}>
          {rule.severity_level?.replace("severity_", "") ?? "—"}
        </Badge>
      </td>
      <td className="px-2">{ruleActionLabel(rule)}</td>
    </tr>
  );
}

function FileRow({ file }: { file: RuleFileSummary }) {
  const [expanded, setExpanded] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const navigate = useNavigate();
  const deleteRuleset = useDeleteRuleset();
  const zebraName = basename(file.path);

  async function handleExportIds() {
    try {
      const detail = await api.get<RuleFileDetailResponse>(`/rules/${encodeURIComponent(file.name)}`);
      const xml = await api.post<string>("/builder/ids-export", { ruleset: detail.ruleset });
      const blob = new Blob([xml], { type: "application/xml" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${zebraName.replace(/\.ya?ml$/, "")}.ids`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch (err) {
      toast.error(String(err));
    }
  }

  const row = (
    <tr className="h-[34px] border-t border-[var(--border)]">
      <td className="px-2">
        <button
          className="flex items-center gap-1 text-left"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
        >
          {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          <MonoText>{zebraName}</MonoText>
        </button>
      </td>
      <td className="px-2">{file.scenario}</td>
      <td className="px-2">{strings.rules.ruleCount(file.rule_count)}</td>
      <td className="px-2">
        <div className="flex flex-wrap gap-1">
          {file.categories.map((c) => (
            <Badge key={c} variant="muted">
              {c}
            </Badge>
          ))}
        </div>
      </td>
      <td className="px-2 text-caption">{formatDateTime(file.mtime)}</td>
      <td className="px-2">
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => navigate(`/rule-builder?file=${encodeURIComponent(file.name)}`)}
          >
            {strings.rules.edit}
          </Button>
          <Button variant="ghost" size="sm" onClick={handleExportIds}>
            <Download size={14} />
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setConfirmDelete(true)}>
            {strings.rules.deleteFile}
          </Button>
        </div>
      </td>
    </tr>
  );

  return (
    <>
      {file.error ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <tr className="h-[34px] cursor-default border-t border-[var(--border)] opacity-50">
              <td className="px-2">
                <span className="flex items-center gap-1">
                  <FileWarning size={14} />
                  <MonoText>{zebraName}</MonoText>
                </span>
              </td>
              <td className="px-2" colSpan={5} />
            </tr>
          </TooltipTrigger>
          <TooltipContent>{file.error}</TooltipContent>
        </Tooltip>
      ) : (
        row
      )}
      {expanded && !file.error && <ExpandedRules name={file.name} file={zebraName} />}

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={strings.rules.confirmDeleteTitle}
        description={strings.rules.confirmDeleteBody(zebraName)}
        destructive
        loading={deleteRuleset.isPending}
        onConfirm={() =>
          deleteRuleset.mutate(file.name, { onSuccess: () => setConfirmDelete(false) })
        }
      />
    </>
  );
}

function ExpandedRules({ name, file }: { name: string; file: string }) {
  const { data: detail, isLoading: loading } = useRuleset(name);

  if (loading) {
    return (
      <tr>
        <td colSpan={6} className="px-4 py-2 text-caption">
          {strings.common.loading}
        </td>
      </tr>
    );
  }
  const rules = detail?.ruleset.rules ?? [];
  const geometry = detail?.ruleset.geometry_rules ?? [];
  if (rules.length === 0 && geometry.length === 0) {
    return (
      <tr>
        <td colSpan={6} className="px-4 py-2 text-caption">
          —
        </td>
      </tr>
    );
  }
  return (
    <tr>
      <td colSpan={6} className="bg-[var(--surface-2)] px-4 py-2">
        <table className="w-full text-left text-[13px]">
          <thead>
            <tr className="text-caption">
              <th className="px-2">{strings.rules.columnRuleId}</th>
              <th className="px-2">{strings.rules.columnCategory}</th>
              <th className="px-2">{strings.rules.columnParameter}</th>
              <th className="px-2">{strings.rules.columnRequirement}</th>
              <th className="px-2">{strings.rules.columnSeverity}</th>
              <th className="px-2">{strings.rules.columnActionKind}</th>
            </tr>
          </thead>
          <tbody>
            {rules.map((r) => (
              <RuleRow key={r.id} file={file} rule={r} />
            ))}
            {geometry.length > 0 && (
              <tr className="h-[26px] border-t border-[var(--border)]">
                <td colSpan={6} className="px-2 text-caption">
                  {strings.rules.geometryCount(geometry.length)}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </td>
    </tr>
  );
}

export function RulesPage() {
  const { data, isLoading, isError, error } = useRulesFiles();
  const navigate = useNavigate();
  const [extractOpen, setExtractOpen] = useState(false);
  const files = data?.files ?? [];

  // FE-7 (2026-07 review): error ≠ empty — a dead service must not render
  // "No rule files yet".
  if (isError) {
    return (
      <div className="flex flex-col gap-6 p-4">
        <PageHeader title={strings.rules.title} description={strings.rules.description} />
        <ApiErrorBanner error={error} />
      </div>
    );
  }

  if (!isLoading && files.length === 0) {
    return (
      <div className="flex flex-col gap-6 p-4">
        <PageHeader
          title={strings.rules.title}
          description={strings.rules.description}
          actions={
            <Button variant="outline" onClick={() => setExtractOpen(true)}>
              <FileText size={14} />
              {strings.rules.extractFromPdf}
            </Button>
          }
        />
        <EmptyState
          title={strings.rules.emptyTitle}
          body={strings.rules.emptyBody}
          actionLabel={strings.rules.emptyAction}
          onAction={() => navigate("/rule-builder")}
        />
        <ExtractPdfDialog open={extractOpen} onOpenChange={setExtractOpen} />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 p-4">
      <PageHeader
        title={strings.rules.title}
        description={strings.rules.description}
        actions={
          <>
            <Button variant="outline" onClick={() => setExtractOpen(true)}>
              <FileText size={14} />
              {strings.rules.extractFromPdf}
            </Button>
            <Button onClick={() => navigate("/rule-builder")}>
              <Plus size={14} />
              {strings.rules.newRule}
            </Button>
          </>
        }
      />
      <div className="card overflow-x-auto">
        <table className="w-full text-left text-[13px]">
          <thead>
            <tr className="h-8 border-b border-[var(--border)] text-caption">
              <th className="px-2">{strings.rules.columnFile}</th>
              <th className="px-2">{strings.rules.columnScenario}</th>
              <th className="px-2">{strings.rules.columnRuleCount}</th>
              <th className="px-2">{strings.rules.columnCategories}</th>
              <th className="px-2">{strings.rules.columnUpdated}</th>
              <th className="px-2" />
            </tr>
          </thead>
          <tbody>
            {files.map((f) => (
              <FileRow key={f.name} file={f} />
            ))}
          </tbody>
        </table>
      </div>
      <ExtractPdfDialog open={extractOpen} onOpenChange={setExtractOpen} />
    </div>
  );
}
