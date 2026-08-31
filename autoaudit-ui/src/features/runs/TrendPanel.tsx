import { Line, LineChart, ResponsiveContainer, Tooltip, YAxis } from "recharts";
import { StatTile } from "@/components/StatTile";
import { strings } from "@/strings";
import { formatCompliancePct } from "@/lib/format";
import type { TrendResponse } from "@/api/types";

export function TrendPanel({ trend }: { trend: TrendResponse | undefined }) {
  const points = trend?.points ?? [];
  if (points.length === 0) {
    return <p className="text-[var(--ink-muted)]">{strings.trend.empty}</p>;
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="h-16 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={points}>
            <YAxis hide domain={["dataMin", "dataMax"]} />
            <Tooltip
              formatter={(value) => formatCompliancePct(Number(value))}
              labelFormatter={() => ""}
            />
            <Line
              type="monotone"
              dataKey="compliance_pct"
              stroke="var(--primary)"
              strokeWidth={2}
              dot={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
      {trend?.diff_latest && (
        <div className="flex gap-2">
          <StatTile label={strings.trend.resolved} value={trend.diff_latest.resolved} color="var(--ok)" />
          <StatTile label={strings.trend.new} value={trend.diff_latest.new} color="var(--fail)" />
          <StatTile label={strings.trend.persistent} value={trend.diff_latest.persistent} color="var(--warn)" />
        </div>
      )}
    </div>
  );
}
