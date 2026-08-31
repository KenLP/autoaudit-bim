import { useState } from "react";
import { Lock } from "lucide-react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Button } from "@/components/ui/button";
import { strings } from "@/strings";
import { useCategories, useParams } from "@/api/hooks";
import type { RuleFormState } from "../lib/ruleForm";

const OTHER_VALUE = "__other__";

export interface ScopeSectionProps {
  state: RuleFormState;
  onChange: (patch: Partial<RuleFormState>) => void;
}

/** "1 Scope" — category + parameter (grounded via the param catalog, spec
 *  3b §8) + the universal scope filter. */
export function ScopeSection({ state, onChange }: ScopeSectionProps) {
  const { data: categoriesData } = useCategories();
  const { data: paramsData } = useParams(state.category || undefined);
  const [filterOpen, setFilterOpen] = useState(!!state.scopeFilterParam);
  const [usingOther, setUsingOther] = useState(!!state.boundParameter);

  const categories = categoriesData?.categories ?? [];
  const params = paramsData?.params ?? [];
  const note = categories.find((c) => c.key === state.category)?.note;
  const selectedSpec = params.find((p) => p.name === state.parameter);

  return (
    <section className="card flex flex-col gap-3 p-3">
      <div className="text-section-title">{strings.builder.scopeTitle}</div>
      <div className="grid grid-cols-2 gap-3">
        <label className="flex flex-col gap-1">
          <span className="text-caption">{strings.builder.category}</span>
          <Select
            value={state.category || undefined}
            onValueChange={(v) => {
              setUsingOther(false);
              onChange({ category: v, parameter: "", boundParameter: "" });
            }}
          >
            <SelectTrigger>
              <SelectValue placeholder={strings.builder.category} />
            </SelectTrigger>
            <SelectContent>
              {categories.map((c) => (
                // value is the DISPLAY label ("Doors"), not the lowercase
                // catalog `key` ("doors") — draft_rule() and every saved
                // rules.*.yaml store category as the display label, and the
                // rules engine matches elements against that same string.
                // Using `key` here made state.category ("Doors") never
                // match any SelectItem's value ("doors"): the dropdown
                // showed nothing after Generate even though the category
                // was correctly captured underneath (2026-08-24).
                <SelectItem key={c.key} value={c.label}>
                  {c.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-caption">{strings.builder.parameter}</span>
          {usingOther ? (
            <Input
              value={state.boundParameter}
              onChange={(e) =>
                onChange({ boundParameter: e.target.value, parameter: e.target.value })
              }
              placeholder={strings.builder.parameterOtherLabel}
            />
          ) : (
            <Select
              value={state.parameter || undefined}
              onValueChange={(v) => {
                if (v === OTHER_VALUE) {
                  setUsingOther(true);
                  onChange({ parameter: "", boundParameter: "" });
                  return;
                }
                onChange({ parameter: v, boundParameter: "" });
              }}
            >
              <SelectTrigger>
                <SelectValue placeholder={strings.builder.parameter} />
              </SelectTrigger>
              <SelectContent>
                {params.map((p) => (
                  <SelectItem key={p.name} value={p.name}>
                    {p.name} · {p.binding} · {p.dimension}
                    {!p.writable ? ` · ${strings.builder.readOnly}` : ""}
                  </SelectItem>
                ))}
                <SelectItem value={OTHER_VALUE}>{strings.builder.parameterOther}</SelectItem>
              </SelectContent>
            </Select>
          )}
        </label>
      </div>
      {note && <p className="text-caption">{note}</p>}
      {selectedSpec && !selectedSpec.writable && (
        <span className="inline-flex w-fit items-center gap-1 text-caption text-[var(--warn)]">
          <Lock size={12} /> {strings.builder.readOnlyTooltip}
        </span>
      )}
      {usingOther && <p className="text-caption">{strings.builder.boundParameterCaption}</p>}

      <Collapsible open={filterOpen} onOpenChange={setFilterOpen}>
        <CollapsibleTrigger asChild>
          <Button variant="ghost" size="sm" className="w-fit">
            {strings.builder.scopeFilterToggle}
          </Button>
        </CollapsibleTrigger>
        <CollapsibleContent className="flex flex-col gap-2 pt-2">
          <p className="text-caption">{strings.builder.scopeFilterHint}</p>
          <div className="grid grid-cols-2 gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-caption">{strings.builder.scopeFilterParam}</span>
              <Input
                value={state.scopeFilterParam}
                onChange={(e) => onChange({ scopeFilterParam: e.target.value })}
                placeholder="IsExternal"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-caption">{strings.builder.scopeFilterPattern}</span>
              <Input
                value={state.scopeFilterPattern}
                onChange={(e) => onChange({ scopeFilterPattern: e.target.value })}
                placeholder="(?i)^(true|yes)$"
              />
            </label>
          </div>
        </CollapsibleContent>
      </Collapsible>
    </section>
  );
}
