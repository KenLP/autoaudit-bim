import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { AlertTriangle } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { strings } from "@/strings";
import { useParams, useRuleset, useValidateRule } from "@/api/hooks";
import { basename } from "@/lib/path";
import {
  emptyGeometryFormState,
  emptyRuleFormState,
  formToRule,
  geometryFormToRule,
  geometryRuleToForm,
  ruleToForm,
  type GeometryFormState,
  type RuleFormState,
} from "./lib/ruleForm";
import { computeCanSave } from "./lib/canSave";
import { DraftSection } from "./sections/Draft";
import { ScopeSection } from "./sections/Scope";
import { CheckSection } from "./sections/Check";
import { SeveritySection } from "./sections/Severity";
import { ActionSection } from "./sections/Action";
import { SaveSection } from "./sections/Save";
import { GeometryForm } from "./GeometryForm";
import type { GeometryRuleDict, RuleDict } from "@/api/types";

const VALIDATE_DEBOUNCE_MS = 500;

export function BuilderPage() {
  const [searchParams] = useSearchParams();
  const fileParam = searchParams.get("file") ?? undefined;
  const ruleParam = searchParams.get("rule") ?? undefined;

  const { data: fileData } = useRuleset(fileParam);

  const [kind, setKind] = useState<"parameter" | "geometry">("parameter");
  const [ruleState, setRuleState] = useState<RuleFormState>(() => emptyRuleFormState());
  const [geoState, setGeoState] = useState<GeometryFormState>(() => emptyGeometryFormState());
  const [id, setId] = useState("");
  const [description, setDescription] = useState("");
  const [scenario, setScenario] = useState(fileData?.ruleset.scenario ?? "");
  const [legacyBanner, setLegacyBanner] = useState<RuleDict | null>(null);
  const [loadedFromFile, setLoadedFromFile] = useState(false);
  // FE-10 (2026-07 review): the section components (Scope/Action) derive
  // LOCAL state from the form state ONCE at mount (mapRaw textarea,
  // usingOther toggle, filter collapsible) — a rule loaded async after
  // mount left them stale (empty map textarea → save silently WIPED the
  // rule's normalize_map). Bumping this epoch remounts the sections via
  // `key`, so they re-derive from the freshly loaded state.
  const [formEpoch, setFormEpoch] = useState(0);

  // Navigating to a different ?file=/?rule= within the mounted page must
  // re-run the load below — `loadedFromFile` otherwise pinned the FIRST
  // loaded rule forever (FE-10).
  useEffect(() => {
    setLoadedFromFile(false);
    setLegacyBanner(null);
  }, [fileParam, ruleParam]);

  // Scenario follows the loaded FILE — also when entering with only ?file=
  // (Rules page "Edit"), which the rule-load effect below early-returns on
  // (FE-10: Save used to propose an empty scenario on that path).
  useEffect(() => {
    if (fileData?.ruleset.scenario) setScenario(fileData.ruleset.scenario);
  }, [fileData]);

  // Load-to-edit: ?file=&rule= -> fetch the ruleset, find the rule, map it
  // onto form state (or show the legacy read-only banner).
  useEffect(() => {
    if (!fileData || !ruleParam || loadedFromFile) return;
    setLoadedFromFile(true);
    const rule = fileData.ruleset.rules.find((r) => r.id === ruleParam);
    if (rule) {
      const result = ruleToForm(rule);
      if (result.legacy) {
        setLegacyBanner(result.raw);
      } else {
        setRuleState(result.state);
        setId(result.state.id);
        setDescription(result.state.description);
        setKind("parameter");
        setFormEpoch((e) => e + 1);
      }
      return;
    }
    const geo = fileData.ruleset.geometry_rules.find((g) => g.id === ruleParam);
    if (geo) {
      const geoForm = geometryRuleToForm(geo);
      setGeoState(geoForm);
      setId(geoForm.id);
      setDescription(geoForm.description);
      setKind("geometry");
      setFormEpoch((e) => e + 1);
    }
  }, [fileData, ruleParam, loadedFromFile]);

  function applyDraft(rule: RuleDict, warnings: string[]) {
    // A clearance sentence now drafts a GEOMETRY rule (router, 2026-08-26).
    // `check_type` is the discriminator: only GeometryRule has it. Same
    // mapping the load-to-edit path already uses for a geometry rule from a
    // file, so there is one way to get a geometry rule onto the form.
    if (typeof (rule as { check_type?: unknown }).check_type === "string") {
      const geoForm = geometryRuleToForm(rule as unknown as GeometryRuleDict);
      setGeoState(geoForm);
      setId(geoForm.id);
      setDescription(geoForm.description);
      setKind("geometry");
      setFormEpoch((e) => e + 1);
      warnings.forEach((w) => console.warn("[builder draft]", w));
      return;
    }
    const result = ruleToForm(rule);
    if (result.legacy) {
      setLegacyBanner(result.raw);
      return;
    }
    setRuleState(result.state);
    setId(result.state.id);
    setDescription(result.state.description);
    setFormEpoch((e) => e + 1); // draft replaces the form wholesale too
    warnings.forEach((w) => {
      // eslint-disable-next-line no-console
      console.warn("[builder draft]", w);
    });
  }

  function updateRule(patch: Partial<RuleFormState>) {
    setRuleState((s) => ({ ...s, ...patch }));
  }
  function updateGeo(patch: Partial<GeometryFormState>) {
    setGeoState((s) => ({ ...s, ...patch }));
  }

  // S-08: these were object literals rebuilt on every render, so the two
  // `useMemo`s below never memoised anything — `formToRule` ran every render
  // and handed a NEW `rule` identity to everything downstream. No wrong
  // behaviour; just a cache that claimed to be one. Memoise the forms on the
  // state they actually derive from and the memos below start working.
  const currentRuleForm: RuleFormState = useMemo(
    () => ({ ...ruleState, id, description }),
    [ruleState, id, description],
  );
  const currentGeoForm: GeometryFormState = useMemo(
    () => ({ ...geoState, id, description }),
    [geoState, id, description],
  );

  const isGeometry = kind === "geometry";
  const rule = useMemo(() => formToRule(currentRuleForm), [currentRuleForm]);
  const geoRule = useMemo(() => geometryFormToRule(currentGeoForm), [currentGeoForm]);

  const { data: paramsData } = useParams(ruleState.category || undefined);
  const paramSpec = paramsData?.params.find((p) => p.name === ruleState.parameter);

  const validate = useValidateRule();
  // Revision of the draft currently on screen. `validate.data` is the
  // LAST-RESOLVED response, never reset when the inputs change, so without
  // pairing the two, Save could be authorized by a validation that describes
  // a DIFFERENT draft: either the undefined-on-first-paint window (no
  // response yet → zero blocking errors → Save enabled before the server has
  // seen anything) or a stale pass from before the user broke the rule.
  const draftRevision = JSON.stringify(isGeometry ? geoRule : rule);
  const [validatedRevision, setValidatedRevision] = useState<string | null>(null);
  useEffect(() => {
    if (legacyBanner) return;
    const handle = setTimeout(() => {
      const revision = draftRevision;
      validate.mutate(
        {
          rule: isGeometry ? geoRule : rule,
          is_geometry: isGeometry,
        },
        { onSuccess: () => setValidatedRevision(revision) },
      );
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, VALIDATE_DEBOUNCE_MS);
    return () => clearTimeout(handle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(rule), JSON.stringify(geoRule), isGeometry, legacyBanner]);

  const validation = validate.data;
  const blockingErrors = validation?.errors ?? [];
  const basicMissing: string[] = [];
  if (!id.trim()) basicMissing.push("Rule ID is required");
  if (!isGeometry && !ruleState.parameter.trim() && !ruleState.boundParameter.trim()) {
    basicMissing.push("Parameter is required");
  }
  if (isGeometry && !geoState.category.trim()) basicMissing.push("Category is required");

  // The server must have validated THIS draft — not a previous one, and not
  // "nothing yet". PUT /rules re-runs the same validation as the final
  // authority, so this is UX (no confusing 422), not the only guard.
  const validationIsCurrent = validatedRevision === draftRevision;
  const canSave = computeCanSave({
    basicMissing,
    blockingErrors,
    validationIsCurrent,
    legacyBanner: !!legacyBanner,
  });

  if (legacyBanner) {
    return (
      <div className="flex flex-col gap-4 p-4">
        <PageHeader
          title={strings.builder.titleEdit}
          description={fileParam ? `${basename(fileParam)} · ${legacyBanner.id}` : undefined}
        />
        <div className="card flex items-start gap-2 border-[var(--warn)] p-3">
          <AlertTriangle size={16} className="mt-0.5 shrink-0 text-[var(--warn)]" />
          <div className="flex flex-col gap-1">
            <span className="text-[13px]">{strings.builder.requirementLegacyBanner}</span>
            <pre className="max-w-2xl overflow-x-auto rounded-[var(--radius)] bg-[var(--surface-2)] p-2 text-[11.5px] font-mono-val">
              {JSON.stringify(legacyBanner, null, 2)}
            </pre>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 p-4 pb-24">
      <PageHeader
        title={ruleParam ? strings.builder.titleEdit : strings.builder.title}
        description={fileParam ? basename(fileParam) : undefined}
      />

      {!ruleParam && <DraftSection onApplyDraft={applyDraft} />}

      <Tabs value={kind} onValueChange={(v) => setKind(v as "parameter" | "geometry")}>
        <TabsList>
          <TabsTrigger value="parameter">{strings.builder.ruleTab}</TabsTrigger>
          <TabsTrigger value="geometry">{strings.builder.geometryTab}</TabsTrigger>
        </TabsList>
      </Tabs>

      <div className="card flex flex-col gap-3 p-3">
        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-caption">{strings.builder.idLabel}</span>
            <Input className="font-mono-val" value={id} onChange={(e) => setId(e.target.value)} />
            <span className="text-caption">{strings.builder.idHint}</span>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-caption">{strings.builder.descriptionLabel}</span>
            <Textarea rows={1} value={description} onChange={(e) => setDescription(e.target.value)} />
          </label>
        </div>
      </div>

      {isGeometry ? (
        <GeometryForm key={`geo-${formEpoch}`} state={currentGeoForm} onChange={updateGeo} />
      ) : (
        <>
          <ScopeSection key={`scope-${formEpoch}`} state={currentRuleForm} onChange={updateRule} />
          <CheckSection key={`check-${formEpoch}`} state={currentRuleForm} onChange={updateRule} />
          <SeveritySection key={`sev-${formEpoch}`} state={currentRuleForm} onChange={updateRule} />
          <ActionSection
            key={`action-${formEpoch}`}
            state={currentRuleForm}
            onChange={updateRule}
            paramSpec={paramSpec}
          />
        </>
      )}

      <SaveSection
        buildRuleset={(scenarioName) =>
          isGeometry
            ? {
                scenario: scenarioName,
                target_category: geoState.category,
                rules: [],
                geometry_rules: [geoRule],
              }
            : {
                scenario: scenarioName,
                target_category: ruleState.category,
                rules: [rule],
                geometry_rules: [],
              }
        }
        disabled={!canSave}
        scenario={scenario}
        onScenarioChange={setScenario}
      />

      <div
        className="fixed bottom-0 left-12 right-0 z-20 border-t border-[var(--border)] bg-[var(--surface)] px-4 py-2"
        data-testid="validation-footer"
      >
        {basicMissing.length > 0 || blockingErrors.length > 0 ? (
          <div className="flex flex-col gap-0.5">
            <span className="text-caption font-semibold text-[var(--fail)]">
              {strings.builder.validationFooterTitle}
            </span>
            <ul className="text-[13px] text-[var(--fail)]">
              {basicMissing.map((m) => (
                <li key={m}>{m}</li>
              ))}
              {blockingErrors.map((e, i) => (
                <li key={i}>
                  {e.field}: {e.message}
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <span className="text-caption text-[var(--ok)]">{strings.builder.validationFooterOk}</span>
        )}
        {validation && validation.warnings.length > 0 && (
          <ul className="text-[13px] text-[var(--warn)]">
            {validation.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
