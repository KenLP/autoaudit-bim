import { useState } from "react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { LookupEditor } from "../LookupEditor";
import { strings } from "@/strings";
import { useLookups } from "@/api/hooks";
import { OFFERED_REQUIREMENTS } from "@/api/types";
import type { ComparisonOperator } from "@/api/types";
import type { RuleFormState } from "../lib/ruleForm";

const OPERATORS: ComparisonOperator[] = [">=", ">", "<=", "<", "==", "!="];
const NEW_LOOKUP = "__new__";

export interface CheckSectionProps {
  state: RuleFormState;
  onChange: (patch: Partial<RuleFormState>) => void;
}

/** "2 Check" — requirement dropdown + the conditional sub-form for
 *  whichever of the 6 offered requirements is selected. */
export function CheckSection({ state, onChange }: CheckSectionProps) {
  const { data: lookupsData } = useLookups();
  const [creatingLookup, setCreatingLookup] = useState(false);
  const lookups = lookupsData?.lookups ?? [];
  const isCanonical = state.requirement === "canonical_format";

  return (
    <section className="card flex flex-col gap-3 p-3">
      <div className="text-section-title">{strings.builder.checkTitle}</div>
      <label className="flex max-w-sm flex-col gap-1">
        <span className="text-caption">{strings.builder.requirement}</span>
        <Select
          value={state.requirement}
          onValueChange={(v) => onChange({ requirement: v as RuleFormState["requirement"] })}
        >
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {OFFERED_REQUIREMENTS.map((r) => (
              <SelectItem key={r} value={r}>
                {strings.builder.requirementOptions[r]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </label>

      {isCanonical && <p className="text-caption">{strings.builder.canonicalCaption}</p>}
      {state.requirement === "unique_in_set" && (
        <p className="text-caption">{strings.builder.uniqueCaption}</p>
      )}

      {state.requirement === "matches_regex" && (
        <div className="flex flex-col gap-2">
          <label className="flex flex-col gap-1">
            <span className="text-caption">{strings.builder.patternLabel}</span>
            <Input
              className="font-mono-val"
              value={state.pattern}
              onChange={(e) => onChange({ pattern: e.target.value })}
              placeholder="^[A-Z]-\d{3}$"
            />
          </label>
          <div className="flex flex-wrap gap-4">
            <label className="flex items-center gap-2 text-[13px]">
              <Checkbox
                checked={state.patternNegate}
                onCheckedChange={(v) =>
                  onChange({ patternNegate: !!v, patternSkipIfEmpty: v ? false : state.patternSkipIfEmpty })
                }
              />
              {strings.builder.patternNegate}
            </label>
            <label className="flex items-center gap-2 text-[13px]">
              <Checkbox
                checked={state.patternSkipIfEmpty}
                disabled={state.patternNegate}
                onCheckedChange={(v) => onChange({ patternSkipIfEmpty: !!v })}
              />
              {strings.builder.patternSkipIfEmpty}
            </label>
          </div>
        </div>
      )}

      {state.requirement === "numeric_compare" && (
        <div className="grid grid-cols-3 gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-caption">{strings.builder.operator}</span>
            <Select
              value={state.operator}
              onValueChange={(v) => onChange({ operator: v as ComparisonOperator })}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {OPERATORS.map((op) => (
                  <SelectItem key={op} value={op}>
                    {op}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-caption">{strings.builder.threshold}</span>
            <Input
              inputMode="decimal"
              value={state.threshold}
              onChange={(e) => onChange({ threshold: e.target.value })}
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-caption">{strings.builder.unit}</span>
            <Input value={state.unit} onChange={(e) => onChange({ unit: e.target.value })} placeholder="mm" />
          </label>
          {(state.operator === ">=" || state.operator === ">") &&
            (state.threshold === "" || Number(state.threshold) === 0) && (
              <p className="col-span-3 text-caption text-[var(--warn)]">
                {strings.builder.thresholdZeroWarning}
              </p>
            )}
        </div>
      )}

      {state.requirement === "relation_compare" && (
        <div className="flex flex-col gap-3">
          <label className="flex max-w-sm flex-col gap-1">
            <span className="text-caption">{strings.builder.relationMode}</span>
            <Select
              value={state.relationMode}
              onValueChange={(v) => onChange({ relationMode: v as RuleFormState["relationMode"] })}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="direct">{strings.builder.relationModeDirect}</SelectItem>
                <SelectItem value="lookup">{strings.builder.relationModeLookup}</SelectItem>
              </SelectContent>
            </Select>
          </label>

          {state.relationMode === "direct" ? (
            <div className="grid grid-cols-3 gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-caption">{strings.builder.otherParam}</span>
                <Input
                  value={state.otherParam}
                  onChange={(e) => onChange({ otherParam: e.target.value })}
                  placeholder={strings.builder.otherParamHint}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-caption">{strings.builder.operator}</span>
                <Select
                  value={state.operator}
                  onValueChange={(v) => onChange({ operator: v as ComparisonOperator })}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {OPERATORS.map((op) => (
                      <SelectItem key={op} value={op}>
                        {op}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-caption">{strings.builder.compareKind}</span>
                <Select
                  value={state.compareKind}
                  onValueChange={(v) => onChange({ compareKind: v as RuleFormState["compareKind"] })}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="numeric">{strings.builder.compareKindOptions.numeric}</SelectItem>
                    <SelectItem value="fire_rating">
                      {strings.builder.compareKindOptions.fire_rating}
                    </SelectItem>
                    <SelectItem value="string">{strings.builder.compareKindOptions.string}</SelectItem>
                  </SelectContent>
                </Select>
              </label>
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              <div className="grid grid-cols-2 gap-3">
                <label className="flex flex-col gap-1">
                  <span className="text-caption">{strings.builder.lookupTable}</span>
                  <Select
                    value={creatingLookup ? NEW_LOOKUP : state.lookup || undefined}
                    onValueChange={(v) => {
                      if (v === NEW_LOOKUP) {
                        setCreatingLookup(true);
                        return;
                      }
                      setCreatingLookup(false);
                      onChange({ lookup: v });
                    }}
                  >
                    <SelectTrigger>
                      <SelectValue placeholder={strings.builder.lookupTable} />
                    </SelectTrigger>
                    <SelectContent>
                      {lookups.map((l) => (
                        <SelectItem key={l.name} value={l.name}>
                          {l.name}
                        </SelectItem>
                      ))}
                      <SelectItem value={NEW_LOOKUP}>{strings.builder.lookupNew}</SelectItem>
                    </SelectContent>
                  </Select>
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-caption">{strings.builder.compareKind}</span>
                  <Select
                    value={state.compareKind}
                    onValueChange={(v) => onChange({ compareKind: v as RuleFormState["compareKind"] })}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="numeric">{strings.builder.compareKindOptions.numeric}</SelectItem>
                      <SelectItem value="fire_rating">
                        {strings.builder.compareKindOptions.fire_rating}
                      </SelectItem>
                      <SelectItem value="string">{strings.builder.compareKindOptions.string}</SelectItem>
                    </SelectContent>
                  </Select>
                </label>
              </div>
              {(creatingLookup || lookups.length === 0) && (
                <LookupEditor
                  onSaved={(name) => {
                    setCreatingLookup(false);
                    onChange({ lookup: name });
                  }}
                />
              )}
              {!creatingLookup && lookups.length === 0 && (
                <p className="text-caption">{strings.builder.lookupMissing}</p>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
