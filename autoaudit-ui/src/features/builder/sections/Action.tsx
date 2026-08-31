import { useState } from "react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Checkbox } from "@/components/ui/checkbox";
import { NormalizePreview } from "../NormalizePreview";
import { ReferenceEditor } from "../ReferenceEditor";
import { strings } from "@/strings";
import { useReferences } from "@/api/hooks";
import type { HandleOption, NormalizeKindOption, WriteTargetOption } from "../lib/ruleForm";
import type { RuleFormState } from "../lib/ruleForm";
import type { ParamEntry } from "@/api/types";

const HANDLE_OPTIONS: HandleOption[] = [
  "issue",
  "normalize",
  "compose_template",
  "inherit_from_host",
  "set_fixed",
];
const WRITE_TARGETS: WriteTargetOption[] = ["auto", "instance", "type", "family", "rename_type"];
const NORMALIZE_KINDS: NormalizeKindOption[] = [
  "auto",
  "duration",
  "length",
  "area",
  "fire_rating",
  "family_name",
  "template",
  "map",
  "reference",
];
const NEW_REFERENCE = "__new__";

function parseMapLines(raw: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of raw.split("\n")) {
    if (!line.includes("=")) continue;
    const [k, v] = line.split("=");
    const key = k?.trim().toLowerCase();
    const value = v?.trim();
    if (key && value) out[key] = value;
  }
  return out;
}

function mapToLines(map: Record<string, string>): string {
  return Object.entries(map)
    .map(([k, v]) => `${k} = ${v}`)
    .join("\n");
}

export interface ActionSectionProps {
  state: RuleFormState;
  onChange: (patch: Partial<RuleFormState>) => void;
  /** The catalog spec for `state.parameter`, when it's a catalog pick (not
   *  the "Other" escape hatch) — used to refuse a read-only write target. */
  paramSpec: ParamEntry | undefined;
}

/** "4 Action" — the ONE handle selector (issue vs 4 auto-fix strategies),
 *  hidden for canonical_format (its fix is inherent — see app.py `is_canon`
 *  branch), plus the write-target / normalize-kind sub-forms. */
export function ActionSection({ state, onChange, paramSpec }: ActionSectionProps) {
  const isCanonical = state.requirement === "canonical_format";
  const effectiveHandle: HandleOption = isCanonical ? "normalize" : state.handle;
  const { data: referencesData } = useReferences();
  const references = referencesData?.references ?? [];
  const [creatingReference, setCreatingReference] = useState(false);
  const [mapRaw, setMapRaw] = useState(mapToLines(state.normalizeMap));

  const showFixSubform = effectiveHandle !== "issue";
  const readOnlyBlock = paramSpec && !paramSpec.writable && showFixSubform;

  return (
    <section className="card flex flex-col gap-3 p-3">
      <div className="text-section-title">{strings.builder.actionTitle}</div>

      {isCanonical ? (
        <p className="text-caption">{strings.builder.canonicalCaption}</p>
      ) : (
        <label className="flex max-w-md flex-col gap-1">
          <span className="text-caption">{strings.builder.actionSelect}</span>
          <Select
            value={state.handle}
            onValueChange={(v) => onChange({ handle: v as HandleOption })}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {HANDLE_OPTIONS.map((h) => (
                <SelectItem key={h} value={h}>
                  {strings.builder.actionOptions[h]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <span className="text-caption">{strings.builder.actionHint}</span>
        </label>
      )}

      {readOnlyBlock && <p className="text-[13px] text-[var(--fail)]">{strings.builder.writeTargetReadOnly}</p>}

      {showFixSubform && (
        <div className="flex flex-col gap-3">
          <label className="flex max-w-xs flex-col gap-1">
            <span className="text-caption">{strings.builder.writeTarget}</span>
            <Select
              value={state.writeTarget}
              onValueChange={(v) => onChange({ writeTarget: v as WriteTargetOption })}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {WRITE_TARGETS.map((t) => (
                  <SelectItem key={t} value={t}>
                    {strings.builder.writeTargetOptions[t]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {state.writeTarget === "auto" && (
              <span className="text-caption">{strings.builder.writeTargetAutoCaption}</span>
            )}
          </label>

          {effectiveHandle === "normalize" && (
            <div className="flex flex-col gap-3 border-t border-[var(--border)] pt-3">
              <label className="flex max-w-xs flex-col gap-1">
                <span className="text-caption">{strings.builder.normalizeKind}</span>
                <Select
                  value={state.normalizeKind}
                  onValueChange={(v) => onChange({ normalizeKind: v as NormalizeKindOption })}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {NORMALIZE_KINDS.map((k) => (
                      <SelectItem key={k} value={k}>
                        {strings.builder.normalizeKindOptions[k]}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </label>

              {state.normalizeKind === "auto" && (
                <p className="text-caption">{strings.builder.normalizeAutoCaption}</p>
              )}

              {state.normalizeKind === "map" && (
                <label className="flex flex-col gap-1">
                  <span className="text-caption">{strings.builder.normalizeMapTitle}</span>
                  <Textarea
                    rows={3}
                    className="font-mono-val"
                    value={mapRaw}
                    onChange={(e) => {
                      setMapRaw(e.target.value);
                      onChange({ normalizeMap: parseMapLines(e.target.value) });
                    }}
                    placeholder="NR = Not Rated"
                  />
                  <span className="text-caption">{strings.builder.normalizeMapHint}</span>
                </label>
              )}

              {state.normalizeKind === "reference" && (
                <div className="flex flex-col gap-2">
                  <label className="flex max-w-xs flex-col gap-1">
                    <span className="text-caption">{strings.builder.normalizeReference}</span>
                    <Select
                      value={creatingReference ? NEW_REFERENCE : state.normalizeReference || undefined}
                      onValueChange={(v) => {
                        if (v === NEW_REFERENCE) {
                          setCreatingReference(true);
                          return;
                        }
                        setCreatingReference(false);
                        onChange({ normalizeReference: v });
                      }}
                    >
                      <SelectTrigger>
                        <SelectValue placeholder={strings.builder.normalizeReference} />
                      </SelectTrigger>
                      <SelectContent>
                        {references.map((r) => (
                          <SelectItem key={r.name} value={r.name}>
                            {r.name}
                          </SelectItem>
                        ))}
                        <SelectItem value={NEW_REFERENCE}>{strings.builder.referenceNew}</SelectItem>
                      </SelectContent>
                    </Select>
                  </label>
                  {(creatingReference || references.length === 0) && (
                    <ReferenceEditor
                      onSaved={(name) => {
                        setCreatingReference(false);
                        onChange({ normalizeReference: name });
                      }}
                    />
                  )}
                  {!creatingReference && references.length === 0 && (
                    <p className="text-caption">{strings.builder.referenceMissing}</p>
                  )}
                </div>
              )}

              {state.normalizeKind === "template" && (
                <div className="grid grid-cols-2 gap-3">
                  <label className="flex flex-col gap-1">
                    <span className="text-caption">{strings.builder.normalizeSource}</span>
                    <Input
                      className="font-mono-val"
                      value={state.normalizeSource}
                      onChange={(e) => onChange({ normalizeSource: e.target.value })}
                      placeholder="(?i)^adsk[ _-]*fur[ _-]*(?P<fn>[a-z]+)"
                    />
                    <span className="text-caption">{strings.builder.normalizeSourceHint}</span>
                  </label>
                  <label className="flex flex-col gap-1">
                    <span className="text-caption">{strings.builder.normalizeTargetTemplate}</span>
                    <Input
                      className="font-mono-val"
                      value={state.normalizeFormat}
                      onChange={(e) => onChange({ normalizeFormat: e.target.value })}
                      placeholder="ADSK_Fur_{fn}"
                    />
                  </label>
                </div>
              )}

              {state.normalizeKind === "family_name" && (
                <p className="text-caption">
                  Collapses separators (space/dash) to underscore — nothing else to declare.
                </p>
              )}

              {!["auto", "map", "reference", "template", "family_name"].includes(state.normalizeKind) && (
                <label className="flex max-w-xs flex-col gap-1">
                  <span className="text-caption">{strings.builder.normalizeFormat}</span>
                  <Input
                    className="font-mono-val"
                    value={state.normalizeFormat}
                    onChange={(e) => onChange({ normalizeFormat: e.target.value })}
                    placeholder="{h}-hour"
                  />
                </label>
              )}

              {!["auto", "reference"].includes(state.normalizeKind) && (
                <div className="flex flex-col gap-2">
                  <label className="flex items-center gap-2 text-[13px]">
                    <Checkbox
                      checked={state.inheritHostWhenEmpty}
                      onCheckedChange={(v) => onChange({ inheritHostWhenEmpty: !!v })}
                    />
                    {strings.builder.inheritWhenEmpty}
                  </label>
                  {state.inheritHostWhenEmpty && (
                    <Input
                      className="max-w-xs"
                      value={state.hostParam}
                      onChange={(e) => onChange({ hostParam: e.target.value })}
                      placeholder={strings.builder.hostParam}
                    />
                  )}
                </div>
              )}

              <NormalizePreview
                normalizeKind={state.normalizeKind}
                normalizeFormat={state.normalizeFormat}
                normalizeMap={state.normalizeMap}
                normalizeSource={state.normalizeSource}
                normalizeReference={state.normalizeReference}
                pattern={state.requirement === "matches_regex" ? state.pattern || null : null}
              />
            </div>
          )}

          {effectiveHandle === "compose_template" && (
            <label className="flex flex-col gap-1">
              <span className="text-caption">{strings.builder.composeTemplate}</span>
              <Input
                className="font-mono-val"
                value={state.composeTemplate}
                onChange={(e) => onChange({ composeTemplate: e.target.value })}
                placeholder={strings.builder.composeTemplateHint}
              />
            </label>
          )}

          {effectiveHandle === "inherit_from_host" && (
            <label className="flex max-w-xs flex-col gap-1">
              <span className="text-caption">{strings.builder.hostParam}</span>
              <Input value={state.hostParam} onChange={(e) => onChange({ hostParam: e.target.value })} />
            </label>
          )}

          {effectiveHandle === "set_fixed" && (
            <label className="flex max-w-xs flex-col gap-1">
              <span className="text-caption">{strings.builder.fixedValue}</span>
              <Input value={state.fixedValue} onChange={(e) => onChange({ fixedValue: e.target.value })} />
            </label>
          )}
        </div>
      )}
    </section>
  );
}
