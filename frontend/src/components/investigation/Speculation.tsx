import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/**
 * THE single shared speculation treatment (design doc: first-class speculative
 * edges with distinct hypothesis styling). Apply SPECULATION_BORDER_CLASS to
 * any container (timeline chip, chain step, actor card) and pair it with the
 * HypothesisBadge. Grounded items keep their normal solid borders.
 */
export const SPECULATION_BORDER_CLASS =
  "border-dashed border-amber-500/60 dark:border-amber-500/40";

export function HypothesisBadge({ className }: { className?: string }) {
  return (
    <Badge
      variant="outline"
      className={cn(
        "border-dashed border-amber-500/60 text-amber-700 dark:border-amber-500/40 dark:text-amber-400",
        className,
      )}
    >
      hypothesis
    </Badge>
  );
}
