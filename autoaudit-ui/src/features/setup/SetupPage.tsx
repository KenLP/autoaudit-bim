import { useState } from "react";
import { toast } from "sonner";
import { ClipboardList, Eye, EyeOff } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { StatusPill, axisStatus } from "@/components/StatusPill";
import { MonoText } from "@/components/MonoText";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EmptyState } from "@/components/EmptyState";
import { strings } from "@/strings";
import {
  useDoctor,
  useHealth,
  useProfiles,
  useSaveEnv,
  useSettings,
  useTestForma,
  useTestRevit,
} from "@/api/hooks";
import { ApiError } from "@/api/client";
import { basename } from "@/lib/path";
import type { AxisName, DoctorCheck, EnvEntry } from "@/api/types";

// Same two connections as the topbar/RunPage pre-flight (LOD/Spatial stay
// hidden — 2026-07-12 feedback).
const CONNECTION_AXES: { key: AxisName; label: string }[] = [
  { key: "revit", label: strings.health.revit },
  { key: "forma", label: strings.health.forma },
];

function groupEnv(env: EnvEntry[]) {
  const forma: EnvEntry[] = [];
  const anthropic: EnvEntry[] = [];
  const other: EnvEntry[] = [];
  for (const entry of env) {
    if (entry.key.startsWith("FORMA_") || entry.key.startsWith("APS_")) {
      forma.push(entry);
    } else if (entry.key === "ANTHROPIC_API_KEY") {
      anthropic.push(entry);
    } else {
      other.push(entry);
    }
  }
  return [
    { label: strings.setup.envGroupForma, items: forma },
    { label: strings.setup.envGroupAnthropic, items: anthropic },
    { label: strings.setup.envGroupOther, items: other },
  ].filter((g) => g.items.length > 0);
}

/** One env key row: masked value + Edit → input (with reveal toggle) → Save
 *  (confirm-gated — this writes local server config, visual language #4). */
function EnvKeyRow({ entry, zebra }: { entry: EnvEntry; zebra: boolean }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const [reveal, setReveal] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const saveEnv = useSaveEnv();

  function startEdit() {
    setValue("");
    setReveal(false);
    setEditing(true);
  }

  function handleConfirm() {
    saveEnv.mutate(
      { key: entry.key, value },
      {
        onSuccess: () => {
          toast(strings.setup.envSave + ": " + entry.key);
          setEditing(false);
          setConfirmOpen(false);
          setValue("");
        },
        onError: (err) => {
          toast.error(err instanceof ApiError ? err.detail : String(err));
        },
      },
    );
  }

  return (
    <>
      <tr
        className={`h-[34px] border-t border-[var(--border)] ${zebra ? "bg-[var(--surface-2)]" : ""}`}
      >
        <td className="px-3">
          <MonoText>{entry.key}</MonoText>
        </td>
        <td className="px-3">
          {entry.set ? (
            <Badge variant="outline" color="var(--ok)">
              {strings.setup.envSet}
            </Badge>
          ) : (
            <Badge variant="outline" color="var(--ink-muted)">
              {strings.setup.envNotSet}
            </Badge>
          )}
        </td>
        <td className="px-3">
          {editing ? (
            <div className="flex items-center gap-1">
              <Input
                type={reveal ? "text" : "password"}
                value={value}
                onChange={(e) => setValue(e.target.value)}
                placeholder={strings.setup.envPlaceholder}
                className="max-w-[220px]"
                autoFocus
              />
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() => setReveal((v) => !v)}
                aria-label={reveal ? strings.setup.envHide : strings.setup.envReveal}
              >
                {reveal ? <EyeOff size={14} /> : <Eye size={14} />}
              </Button>
            </div>
          ) : (
            <MonoText className="text-[var(--ink-muted)]">{entry.masked ?? "—"}</MonoText>
          )}
        </td>
        <td className="px-3">
          {editing ? (
            <div className="flex items-center gap-1">
              <Button
                size="sm"
                disabled={!value.trim() || saveEnv.isPending}
                onClick={() => setConfirmOpen(true)}
              >
                {saveEnv.isPending ? strings.setup.envSaving : strings.setup.envSave}
              </Button>
              <Button variant="outline" size="sm" onClick={() => setEditing(false)}>
                {strings.common.cancel}
              </Button>
            </div>
          ) : (
            <Button variant="ghost" size="sm" onClick={startEdit}>
              {strings.setup.envEdit}
            </Button>
          )}
        </td>
      </tr>
      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={strings.setup.envConfirmTitle}
        description={strings.setup.envConfirmBody(entry.key)}
        loading={saveEnv.isPending}
        onConfirm={handleConfirm}
      />
    </>
  );
}

function DoctorTable({ checks }: { checks: DoctorCheck[] }) {
  const color: Record<DoctorCheck["status"], string> = {
    pass: "var(--ok)",
    warn: "var(--warn)",
    fail: "var(--fail)",
  };
  return (
    <table className="w-full text-left text-[13px]">
      <thead>
        <tr className="h-8 border-b border-[var(--border)] text-caption">
          <th className="px-3">{strings.setup.diagnosticsColumnCheck}</th>
          <th className="px-3">{strings.setup.diagnosticsColumnStatus}</th>
          <th className="px-3">{strings.setup.diagnosticsColumnDetail}</th>
        </tr>
      </thead>
      <tbody>
        {checks.map((c, i) => (
          <tr
            key={c.name}
            className={`h-[34px] border-b border-[var(--border)] last:border-0 ${
              i % 2 === 1 ? "bg-[var(--surface-2)]" : ""
            }`}
          >
            <td className="px-3">{c.name}</td>
            <td className="px-3">
              <Badge variant="outline" color={color[c.status]}>
                {strings.setup.diagnosticsStatus[c.status]}
              </Badge>
            </td>
            <td className="px-3 text-[var(--ink-muted)]">{c.detail}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function SetupPage() {
  const { data: health } = useHealth();
  const { data: profilesData, isLoading } = useProfiles();
  const { data: settings, isLoading: settingsLoading } = useSettings();
  const testForma = useTestForma();
  const testRevit = useTestRevit();
  const doctor = useDoctor();

  const profiles = profilesData?.profiles ?? [];
  const ruleFiles = [...new Set(profiles.flatMap((p) => p.rules))].sort();
  const envGroups = groupEnv(settings?.env ?? []);

  return (
    <div className="flex flex-col gap-6 p-4">
      <PageHeader title={strings.setup.title} description={strings.setup.description} />

      <div className="card flex flex-col gap-3 p-4">
        <div className="text-section-title">{strings.setup.statusTitle}</div>
        <div className="flex flex-wrap gap-4">
          {CONNECTION_AXES.map(({ key, label }) => (
            <StatusPill
              key={key}
              name={label}
              status={axisStatus(health?.axes[key])}
            />
          ))}
        </div>
      </div>

      <div className="card flex flex-col gap-3 p-4">
        <div className="text-section-title">{strings.setup.connectionsTitle}</div>
        <p className="text-caption">{strings.setup.connectionsNote}</p>
        {!settingsLoading && envGroups.length === 0 ? (
          <span className="text-caption">{strings.setup.envEmpty}</span>
        ) : (
          <div className="flex flex-col gap-4">
            {envGroups.map((group) => (
              <div key={group.label} className="flex flex-col gap-1">
                <div className="text-caption font-medium">{group.label}</div>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-[13px]">
                    <thead>
                      <tr className="h-8 border-b border-[var(--border)] text-caption">
                        <th className="px-3">{strings.setup.envColumnKey}</th>
                        <th className="px-3">{strings.setup.envColumnStatus}</th>
                        <th className="px-3">{strings.setup.envColumnValue}</th>
                        <th className="px-3" />
                      </tr>
                    </thead>
                    <tbody>
                      {group.items.map((entry, i) => (
                        <EnvKeyRow key={entry.key} entry={entry} zebra={i % 2 === 1} />
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="card flex flex-col gap-3 p-4">
        <div className="text-section-title">{strings.setup.testTitle}</div>
        <div className="flex flex-wrap items-center gap-3">
          <Button
            variant="outline"
            disabled={testForma.isPending}
            onClick={() => testForma.mutate()}
          >
            {testForma.isPending ? strings.setup.testRunning : strings.setup.testForma}
          </Button>
          {testForma.data && (
            <span style={{ color: testForma.data.ok ? "var(--ok)" : "var(--fail)" }}>
              {testForma.data.message}
            </span>
          )}
          {testForma.isError && (
            <span style={{ color: "var(--fail)" }}>
              {testForma.error instanceof ApiError
                ? testForma.error.detail
                : String(testForma.error)}
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Button
            variant="outline"
            disabled={testRevit.isPending}
            onClick={() => testRevit.mutate()}
          >
            {testRevit.isPending ? strings.setup.testRunning : strings.setup.testRevit}
          </Button>
          {testRevit.data && (
            <span style={{ color: testRevit.data.ok ? "var(--ok)" : "var(--fail)" }}>
              {testRevit.data.message}
              {testRevit.data.version
                ? strings.setup.testVersionSuffix(testRevit.data.version)
                : ""}
            </span>
          )}
          {testRevit.isError && (
            <span style={{ color: "var(--fail)" }}>
              {testRevit.error instanceof ApiError
                ? testRevit.error.detail
                : String(testRevit.error)}
            </span>
          )}
        </div>
      </div>

      <div className="card flex flex-col gap-3 p-4">
        <div className="flex items-center justify-between">
          <div className="text-section-title">{strings.setup.diagnosticsTitle}</div>
          <Button
            variant="outline"
            disabled={doctor.isPending}
            onClick={() => doctor.mutate()}
          >
            {doctor.isPending ? strings.setup.diagnosticsRunning : strings.setup.diagnosticsRun}
          </Button>
        </div>
        {doctor.data ? (
          <DoctorTable checks={doctor.data.checks} />
        ) : (
          <span className="text-caption">{strings.setup.diagnosticsEmpty}</span>
        )}
      </div>

      <div className="card flex flex-col gap-3 p-4">
        <div className="text-section-title">{strings.setup.profilesTitle}</div>
        {!isLoading && profiles.length === 0 ? (
          <EmptyState icon={ClipboardList} title={strings.setup.emptyProfiles} />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[13px]">
              <thead>
                <tr className="h-8 border-b border-[var(--border)] text-caption">
                  <th className="px-3">{strings.setup.columnName}</th>
                  <th className="px-3">{strings.setup.columnRules}</th>
                  <th className="px-3">{strings.setup.columnMode}</th>
                  <th className="px-3">{strings.setup.columnStatus}</th>
                </tr>
              </thead>
              <tbody>
                {profiles.map((p, i) => (
                  <tr
                    key={p.path}
                    className={`h-[34px] border-b border-[var(--border)] last:border-0 ${
                      i % 2 === 1 ? "bg-[var(--surface-2)]" : ""
                    }`}
                  >
                    <td className="px-3">{p.name}</td>
                    <td className="px-3 font-mono-val" title={p.rules.join(", ")}>
                      {p.rules.length > 0 ? p.rules.map(basename).join(", ") : "—"}
                    </td>
                    <td className="px-3">{p.mode}</td>
                    <td className="px-3">
                      {p.error ? (
                        <Badge variant="outline" color="var(--fail)">
                          {p.error}
                        </Badge>
                      ) : (
                        <Badge variant="outline" color="var(--ok)">
                          {strings.setup.statusOk}
                        </Badge>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card flex flex-col gap-2 p-4">
        <div className="text-section-title">{strings.setup.rulesFilesTitle}</div>
        {ruleFiles.length === 0 ? (
          <span className="text-caption">—</span>
        ) : (
          <ul className="flex flex-col gap-1">
            {ruleFiles.map((r) => (
              <li key={r} title={r}>
                <MonoText className="text-[13px]">{basename(r)}</MonoText>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="card flex flex-col gap-1 p-4">
        <div className="text-section-title">{strings.setup.aboutTitle}</div>
        <span className="text-caption">
          {strings.setup.aboutVersion(health?.version ?? "—")}
        </span>
        <span className="text-caption">{strings.setup.aboutDocsNote}</span>
      </div>
    </div>
  );
}
