import * as React from "react";
import { cn } from "@/lib/utils";

/** Element ids / parameter values / run ids always render in the mono font. */
export function MonoText({
  className,
  ...props
}: React.HTMLAttributes<HTMLSpanElement>) {
  return <span className={cn("font-mono-val", className)} {...props} />;
}
