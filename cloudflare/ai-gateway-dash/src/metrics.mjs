/** Display metrics shared by the dashboard server and its embedded client.

  The client script is rendered from these function sources so the browser and
  the Worker always agree on how a quota window or audit flag is interpreted.
 */

export function codexRemainingPercent(raw) {
  if (!raw || typeof raw !== "object") return null;
  if (raw.remaining_percent !== null && raw.remaining_percent !== undefined) {
    const explicit = Number(raw.remaining_percent);
    if (Number.isFinite(explicit)) return Math.max(0, Math.min(100, Math.round(explicit)));
  }
  const used = Number(raw.used_percent);
  if (!Number.isFinite(used)) return null;
  return Math.max(0, Math.min(100, Math.round(100 - used)));
}

export function codexWindowLabel(raw) {
  const mins = Number(raw && raw.window_duration_mins) || 0;
  if (!mins) return "窗口";
  if (mins % 1440 === 0) return Math.round(mins / 1440) + "d";
  if (mins % 60 === 0) return Math.round(mins / 60) + "h";
  return mins + "m";
}

export function codexWindowDisplay(raw, remainingLabel = "剩余") {
  const remaining = codexRemainingPercent(raw);
  if (remaining === null) return "";
  return codexWindowLabel(raw) + " " + remainingLabel + " " + remaining + "%";
}

export function codexRemainingClass(raw) {
  const remaining = codexRemainingPercent(raw);
  if (remaining === null) return "ok";
  return remaining < 20 ? "err" : remaining < 50 ? "warn" : "ok";
}

export function requiresHumanAudit(change) {
  if (!change || typeof change !== "object") return false;
  if (typeof change.human_review_required === "boolean") {
    return change.human_review_required;
  }
  const state = String(change.state || "");
  if ([
    "human_review_required",
    "human_review_pr_opened",
    "human_review_waiting_for_ci",
    "human_review_auto_merge_requested",
  ].includes(state)) return true;
  const action = String(change.action || "");
  const risk = String(change.risk || "");
  const effect = String(change.effect || "");
  return (
    action === "escalate"
    || action === "manual"
    || risk === "critical"
    || risk === "high"
    || effect === "degraded"
    || Boolean(change.rollback_issue_url)
  );
}
