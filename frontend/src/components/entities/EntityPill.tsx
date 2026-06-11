import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import type { EntityType } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * One colorway per entity_type (mirrors ENTITY_TYPES in backend
 * domain/enums.py). Tailwind needs literal class strings, hence the map.
 */
const ENTITY_TYPE_STYLES: Record<
  EntityType,
  { label: string; pill: string; chip: string }
> = {
  person: {
    label: "person",
    pill: "bg-sky-100 text-sky-800 hover:bg-sky-200 dark:bg-sky-950 dark:text-sky-300 dark:hover:bg-sky-900",
    chip: "bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300",
  },
  organization: {
    label: "organization",
    pill: "bg-violet-100 text-violet-800 hover:bg-violet-200 dark:bg-violet-950 dark:text-violet-300 dark:hover:bg-violet-900",
    chip: "bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-300",
  },
  ministry: {
    label: "ministry",
    pill: "bg-indigo-100 text-indigo-800 hover:bg-indigo-200 dark:bg-indigo-950 dark:text-indigo-300 dark:hover:bg-indigo-900",
    chip: "bg-indigo-100 text-indigo-800 dark:bg-indigo-950 dark:text-indigo-300",
  },
  company: {
    label: "company",
    pill: "bg-emerald-100 text-emerald-800 hover:bg-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:hover:bg-emerald-900",
    chip: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
  },
  political_party: {
    label: "party",
    pill: "bg-rose-100 text-rose-800 hover:bg-rose-200 dark:bg-rose-950 dark:text-rose-300 dark:hover:bg-rose-900",
    chip: "bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300",
  },
  law: {
    label: "law",
    pill: "bg-amber-100 text-amber-800 hover:bg-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:hover:bg-amber-900",
    chip: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  },
  scheme: {
    label: "scheme",
    pill: "bg-teal-100 text-teal-800 hover:bg-teal-200 dark:bg-teal-950 dark:text-teal-300 dark:hover:bg-teal-900",
    chip: "bg-teal-100 text-teal-800 dark:bg-teal-950 dark:text-teal-300",
  },
  policy_instrument: {
    label: "policy instrument",
    pill: "bg-cyan-100 text-cyan-800 hover:bg-cyan-200 dark:bg-cyan-950 dark:text-cyan-300 dark:hover:bg-cyan-900",
    chip: "bg-cyan-100 text-cyan-800 dark:bg-cyan-950 dark:text-cyan-300",
  },
  place: {
    label: "place",
    pill: "bg-lime-100 text-lime-800 hover:bg-lime-200 dark:bg-lime-950 dark:text-lime-300 dark:hover:bg-lime-900",
    chip: "bg-lime-100 text-lime-800 dark:bg-lime-950 dark:text-lime-300",
  },
  other: {
    label: "other",
    pill: "bg-muted text-muted-foreground hover:bg-muted/70",
    chip: "bg-muted text-muted-foreground",
  },
};

const FALLBACK = ENTITY_TYPE_STYLES.other;

function styleFor(type: string) {
  return ENTITY_TYPE_STYLES[type as EntityType] ?? FALLBACK;
}

export function entityTypeLabel(type: string): string {
  return styleFor(type).label;
}

/** Small static badge naming the entity_type (entity page header, lists). */
export function EntityTypeChip({
  type,
  className,
}: {
  type: string;
  className?: string;
}) {
  return (
    <Badge variant="secondary" className={cn(styleFor(type).chip, className)}>
      {entityTypeLabel(type)}
    </Badge>
  );
}

/**
 * Clickable entity chip, colored by entity_type, navigating to the entity
 * page. `className` can override size/intensity (co-occurrence cloud).
 */
export function EntityPill({
  entity,
  className,
  title,
}: {
  entity: { id: number; name: string; entity_type: string };
  className?: string;
  title?: string;
}) {
  return (
    <Link
      href={`/entity/${entity.id}`}
      title={title ?? `${entity.name} (${entityTypeLabel(entity.entity_type)})`}
      className={cn(
        "inline-flex max-w-full items-center rounded-full px-2.5 py-0.5 text-xs font-medium transition-colors",
        styleFor(entity.entity_type).pill,
        className,
      )}
    >
      <span className="truncate">{entity.name}</span>
    </Link>
  );
}
