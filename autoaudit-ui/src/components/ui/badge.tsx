import * as React from "react";
import { cn } from "@/lib/utils";

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  /** "solid" = filled dot + tinted bg (bucket use). "outline" = border only (severity use). */
  variant?: "solid" | "outline" | "muted";
  /** CSS color token, e.g. "var(--ok)". */
  color?: string;
}

export function Badge({
  className,
  variant = "outline",
  color,
  style,
  ...props
}: BadgeProps) {
  const base =
    "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11.5px] font-medium leading-none";
  if (variant === "solid") {
    return (
      <span
        className={cn(base, "border", className)}
        style={{
          color,
          borderColor: color,
          background: color ? `color-mix(in srgb, ${color} 12%, transparent)` : undefined,
          ...style,
        }}
        {...props}
      >
        <span
          aria-hidden
          className="inline-block h-1.5 w-1.5 rounded-full"
          style={{ background: color }}
        />
        {props.children}
      </span>
    );
  }
  if (variant === "muted") {
    return (
      <span
        className={cn(base, "border border-[var(--border)] text-[var(--ink-muted)]", className)}
        {...props}
      />
    );
  }
  return (
    <span
      className={cn(base, "border", className)}
      style={{ color, borderColor: color, ...style }}
      {...props}
    />
  );
}
