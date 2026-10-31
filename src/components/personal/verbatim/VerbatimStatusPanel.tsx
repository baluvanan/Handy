import React from "react";
import { useTranslation } from "react-i18next";
import type { VerbatimServiceStatus, WorkerHealth } from "@/bindings";
import { Button } from "../../ui/Button";
import { Alert } from "../../ui/Alert";
import {
  formatBytes,
  formatWhen,
  useVerbatimService,
} from "./useVerbatimService";
import "../i18n";

const HEALTH_TONE: Record<WorkerHealth, string> = {
  ok: "bg-moss-wash text-moss",
  needs_attention: "bg-warning-wash text-warning",
  out_of_date: "bg-warning-wash text-warning",
  not_synced: "bg-warning-wash text-warning",
  broken: "bg-error-wash text-error",
  missing: "bg-error-wash text-error",
  no_uv: "bg-error-wash text-error",
};

/** Whether "Fix" can do something: set up / update the environment or
 * download the model. Given-up recordings need "Retry" instead. */
export const fixable = (s: VerbatimServiceStatus) =>
  ["not_synced", "out_of_date", "broken"].includes(s.health) ||
  (s.health === "needs_attention" &&
    (s.status?.model.state !== "ready" || !s.status?.versions.up_to_date));

/** Runs are possible once the environment is set up. */
export const runnable = (s: VerbatimServiceStatus) =>
  s.health === "ok" || s.health === "needs_attention";

export const HealthBadge: React.FC<{ health: WorkerHealth }> = ({ health }) => {
  const { t } = useTranslation();
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full ${HEALTH_TONE[health]}`}>
      {t(`personal.verbatim.health.${health}`)}
    </span>
  );
};

const Row: React.FC<{ label: string; children: React.ReactNode }> = ({
  label,
  children,
}) => (
  <div className="flex justify-between gap-3 text-xs py-0.5">
    <span className="text-ink-3">{label}</span>
    <span className="text-end tabular-nums select-text">{children}</span>
  </div>
);

/** Insights page: what the verbatim worker did, what's queued and what failed. */
export const VerbatimStatusPanel: React.FC = () => {
  const { t } = useTranslation();
  const { status, settings, loading, refresh, runNow, retry, fix } =
    useVerbatimService();

  if (!status || !settings) return null;
  if (!settings.enabled) {
    return (
      <div className="bg-background border border-line rounded-lg p-3 text-xs text-ink-3">
        {t("personal.verbatim.panel.disabled")}
      </div>
    );
  }

  const w = status.status;
  const run = w?.last_run;
  const busy = status.operation !== null || status.run_in_progress;
  const canFix = fixable(status);

  return (
    <div className="bg-background border border-line rounded-lg p-3 space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">
            {t("personal.verbatim.panel.title")}
          </span>
          <HealthBadge health={status.health} />
        </div>
        <div className="flex gap-2">
          {canFix && (
            <Button
              size="sm"
              variant="primary-soft"
              onClick={fix}
              disabled={busy}
            >
              {t("personal.verbatim.actions.fix")}
            </Button>
          )}
          <Button
            size="sm"
            variant="secondary"
            onClick={runNow}
            disabled={busy || !runnable(status)}
          >
            {status.run_in_progress || w?.running
              ? t("personal.verbatim.actions.running")
              : t("personal.verbatim.actions.runNow")}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={refresh}
            disabled={loading}
          >
            {t("personal.verbatim.actions.refresh")}
          </Button>
        </div>
      </div>

      {status.operation && (
        <Alert variant="info" contained>
          {t("personal.verbatim.operation", { op: status.operation })}
        </Alert>
      )}
      {status.last_operation_error && (
        <Alert variant="error" contained>
          {status.last_operation_error}
        </Alert>
      )}
      {status.error && (
        <Alert variant="error" contained>
          {status.error}
        </Alert>
      )}
      {(w?.attention ?? []).map((a) => (
        <Alert key={a} variant="warning" contained>
          {a}
        </Alert>
      ))}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6">
        <div>
          <Row label={t("personal.verbatim.panel.lastRun")}>
            {run
              ? formatWhen(run.finished_at ?? run.started_at)
              : t("personal.verbatim.panel.never")}
          </Row>
          {run && (
            <Row label={t("personal.verbatim.panel.outcome")}>
              {t(`personal.verbatim.outcome.${run.outcome ?? "running"}`, {
                defaultValue: run.outcome ?? "",
              })}
              {run.message ? ` — ${run.message}` : ""}
            </Row>
          )}
          {run && (
            <Row label={t("personal.verbatim.panel.lastRunCounts")}>
              {t("personal.verbatim.panel.counts", {
                done: run.processed,
                failed: run.failed,
                skipped: run.skipped,
              })}
            </Row>
          )}
          <Row label={t("personal.verbatim.panel.queue")}>
            {w?.queue.pending ?? "—"}
          </Row>
          <Row label={t("personal.verbatim.panel.done")}>
            {w?.queue.done ?? 0}
          </Row>
        </div>
        <div>
          <Row label={t("personal.verbatim.panel.model")}>
            {w
              ? `${w.model.state}${w.model.revision ? ` · ${w.model.revision.slice(0, 8)}` : ""}`
              : "—"}
          </Row>
          <Row label={t("personal.verbatim.panel.disk")}>
            {w
              ? t("personal.verbatim.panel.diskValue", {
                  used: formatBytes(w.disk.worker_bytes),
                  free: formatBytes(w.disk.free_bytes),
                })
              : "—"}
          </Row>
          <Row label={t("personal.verbatim.panel.versions")}>
            {t("personal.verbatim.panel.versionsValue", {
              worker: w?.versions.worker_version ?? "—",
              expected: status.bundled_worker_version ?? "?",
            })}
          </Row>
          <Row label={t("personal.verbatim.panel.launchd")}>
            {w
              ? w.launchd.loaded
                ? t("personal.verbatim.panel.loaded", {
                    exit: w.launchd.last_exit ?? "—",
                  })
                : t("personal.verbatim.panel.notLoaded")
              : "—"}
          </Row>
        </div>
      </div>

      {w && w.failures.length > 0 && (
        <div className="border-t border-line pt-2 space-y-1">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium">
              {t("personal.verbatim.panel.failures", {
                count: w.failures.length,
              })}
            </span>
            <Button size="sm" variant="ghost" onClick={() => retry(null)}>
              {t("personal.verbatim.actions.retryAll")}
            </Button>
          </div>
          <ul className="space-y-1 max-h-40 overflow-y-auto">
            {w.failures.map((f) => (
              <li
                key={f.entry_id}
                className="flex items-start justify-between gap-2 text-xs"
              >
                <span className="min-w-0">
                  <span className="tabular-nums">#{f.entry_id}</span>{" "}
                  <span className="text-ink-3">
                    {f.status === "skipped"
                      ? t("personal.verbatim.panel.skipped", {
                          reason: f.skip_reason ?? "",
                        })
                      : t("personal.verbatim.panel.attempts", {
                          count: f.attempts,
                        })}
                  </span>
                  {f.last_error && (
                    <span
                      className="block text-ink-3 truncate"
                      title={f.last_error}
                    >
                      {f.last_error}
                    </span>
                  )}
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => retry(f.entry_id)}
                >
                  {t("personal.verbatim.actions.retry")}
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
};
