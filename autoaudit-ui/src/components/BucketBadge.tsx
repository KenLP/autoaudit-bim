import { Badge } from "@/components/ui/badge";
import { strings } from "@/strings";
import type { Bucket } from "@/api/types";

const COLOR: Record<Bucket, string> = {
  compliant: "var(--bucket-compliant)",
  non_compliant: "var(--bucket-non-compliant)",
  manual_review: "var(--bucket-manual-review)",
  missing_data: "var(--bucket-missing-data)",
};

export function BucketBadge({ bucket }: { bucket: Bucket }) {
  return (
    <Badge variant="solid" color={COLOR[bucket]}>
      {strings.bucket[bucket]}
    </Badge>
  );
}
