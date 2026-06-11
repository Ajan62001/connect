import { format, formatDistanceToNowStrict, isValid } from "date-fns";

/**
 * Backend timestamps come from SQLite `datetime('now')` — UTC but without a
 * timezone marker ("2026-06-11 04:12:09"). Normalize to an ISO string with a
 * trailing Z unless an offset is already present.
 */
export function parseTimestamp(value: string): Date | null {
  let iso = value.trim();
  if (!iso) return null;
  if (iso.includes(" ") && !iso.includes("T")) iso = iso.replace(" ", "T");
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso);
  const date = new Date(hasZone ? iso : `${iso}Z`);
  return isValid(date) ? date : null;
}

/** "3 hours ago" — empty string when the timestamp is missing or unparsable. */
export function relativeTime(value: string | null | undefined): string {
  if (!value) return "";
  const date = parseTimestamp(value);
  if (!date) return value;
  return formatDistanceToNowStrict(date, { addSuffix: true });
}

/** "11 Jun 2026, 09:42" — for tooltips and metadata panels. */
export function absoluteTime(value: string | null | undefined): string {
  if (!value) return "";
  const date = parseTimestamp(value);
  if (!date) return value;
  return format(date, "d MMM yyyy, HH:mm");
}

/**
 * Date-only values ('YYYY-MM-DD' — event.occurred_on, brief_date) must parse
 * in local time: routing them through `parseTimestamp` would append a Z and
 * produce an invalid date. Falls back to timestamp parsing for safety.
 */
export function parseDateOnly(value: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value.trim());
  if (m) {
    const date = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    return isValid(date) ? date : null;
  }
  return parseTimestamp(value);
}

/** "11 Jun 2026" — for occurred_on / brief_date style fields. */
export function formatDay(value: string | null | undefined): string {
  if (!value) return "";
  const date = parseDateOnly(value);
  return date ? format(date, "d MMM yyyy") : value;
}

/** First 12 hex chars of a content hash, for compact display. */
export function shortHash(hash: string): string {
  return hash.length > 12 ? `${hash.slice(0, 12)}…` : hash;
}

/** Hostname of a URL for compact display; falls back to the raw string. */
export function urlHost(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

/** Human label for a document media_type chip (feed/library/detail). */
const MEDIA_TYPE_LABELS: Record<string, string> = {
  html: "web",
  pdf: "PDF",
  text: "text",
  tweet: "post on X",
  telegram: "Telegram post",
  xlsx: "Spreadsheet",
  docx: "Word doc",
};

export function mediaTypeLabel(value: string): string {
  return MEDIA_TYPE_LABELS[value] ?? value;
}

/**
 * "$0.42" — LLM spend amounts. Sub-cent (but non-zero) values switch to four
 * decimals so a day of cheap Haiku calls doesn't render as "$0.00".
 */
export function formatUsd(value: number): string {
  const digits = value !== 0 && Math.abs(value) < 0.01 ? 4 : 2;
  return `$${value.toFixed(digits)}`;
}
