import React, { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  commands,
  type RetentionPreview,
  type RetentionSettings,
} from "@/bindings";
import { useSettings } from "@/hooks/useSettings";
import { SettingsGroup } from "../../ui/SettingsGroup";
import { ToggleSwitch } from "../../ui/ToggleSwitch";
import { SettingContainer } from "../../ui/SettingContainer";
import { Input } from "../../ui/Input";
import { Button } from "../../ui/Button";
import { Alert } from "../../ui/Alert";
import { Dialog } from "../../ui/Dialog";
import { Dropdown } from "../../ui/Dropdown";
import { fixable, HealthBadge } from "./VerbatimStatusPanel";
import { formatBytes, useVerbatimService } from "./useVerbatimService";
import "../i18n";

const pad = (n: number) => String(n).padStart(2, "0");
const toTime = (h: number, m: number) => `${pad(h)}:${pad(m)}`;
const fromTime = (v: string): [number, number] | null => {
  const m = /^(\d{1,2}):(\d{2})$/.exec(v);
  if (!m) return null;
  return [Number(m[1]), Number(m[2])];
};

/** Settings → Advanced → Verbatim analysis. */
export const PersonalVerbatimSettings: React.FC = () => {
  const { t } = useTranslation();
  const { settings: appSettings } = useSettings();
  const svc = useVerbatimService();
  const s = svc.settings;
  const status = svc.status;
  const [preview, setPreview] = useState<RetentionPreview | null>(null);
  const [pendingRetention, setPendingRetention] =
    useState<RetentionSettings | null>(null);
  const [doctor, setDoctor] = useState<string | null>(null);
  const [doctorBusy, setDoctorBusy] = useState(false);
  const [workerDir, setWorkerDir] = useState(s?.worker_dir ?? "");
  const [uvPath, setUvPath] = useState(s?.uv_path ?? "");

  useEffect(() => {
    setWorkerDir(s?.worker_dir ?? "");
    setUvPath(s?.uv_path ?? "");
  }, [s?.worker_dir, s?.uv_path]);

  if (!s) return null;
  const busy = status?.operation !== null && status?.operation !== undefined;

  const askRetention = async (next: RetentionSettings) => {
    const result = await commands.retentionPreview({ ...next, enabled: true });
    if (result.status === "ok") {
      setPreview(result.data);
      setPendingRetention(next);
    }
  };

  const runDoctor = async () => {
    setDoctorBusy(true);
    const result = await commands.verbatimDoctor();
    setDoctorBusy(false);
    setDoctor(result.status === "ok" ? result.data : result.error);
  };

  const providers = (appSettings?.post_process_providers ?? []).map((p) => ({
    value: p.id,
    label: p.label,
  }));

  return (
    <SettingsGroup
      title={t("personal.verbatim.settings.group")}
      description={t("personal.verbatim.settings.groupDescription")}
    >
      <ToggleSwitch
        checked={s.enabled}
        disabled={busy}
        onChange={(enabled) => void svc.updateSettings({ enabled })}
        label={t("personal.verbatim.settings.enabled.title")}
        description={t("personal.verbatim.settings.enabled.description")}
        descriptionMode="tooltip"
        grouped
      />
      {status && (
        <div className="px-4 py-2 space-y-2">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <HealthBadge health={status.health} />
            {!status.platform_supported && (
              <span className="text-ink-3">
                {t("personal.verbatim.settings.macOnly")}
              </span>
            )}
            {status.operation && (
              <span className="text-ink-3">
                {t("personal.verbatim.operation", { op: status.operation })}
              </span>
            )}
          </div>
          {status.health === "missing" && (
            <Alert variant="error" contained>
              {t("personal.verbatim.settings.missing", {
                dir: status.worker_dir,
              })}
            </Alert>
          )}
          {status.health === "no_uv" && (
            <Alert variant="error" contained>
              {t("personal.verbatim.settings.noUv")}
            </Alert>
          )}
          {status.last_operation_error && (
            <Alert variant="error" contained>
              {status.last_operation_error}
            </Alert>
          )}
          <div className="flex flex-wrap gap-2">
            {fixable(status) && (
              <Button
                size="sm"
                variant="primary-soft"
                onClick={svc.fix}
                disabled={busy}
              >
                {t("personal.verbatim.actions.fix")}
              </Button>
            )}
            <Button size="sm" variant="secondary" onClick={svc.revealLogs}>
              {t("personal.verbatim.actions.revealLogs")}
            </Button>
            <Button
              size="sm"
              variant="secondary"
              onClick={runDoctor}
              disabled={doctorBusy || status.health === "missing"}
            >
              {doctorBusy
                ? t("personal.verbatim.actions.doctorBusy")
                : t("personal.verbatim.actions.doctor")}
            </Button>
          </div>
          {doctor && (
            <pre className="text-xs bg-wash rounded p-2 whitespace-pre-wrap max-h-64 overflow-y-auto select-text">
              {doctor}
            </pre>
          )}
        </div>
      )}

      <SettingContainer
        title={t("personal.verbatim.settings.schedule.title")}
        description={t("personal.verbatim.settings.schedule.description")}
        grouped
      >
        <Input
          type="time"
          variant="compact"
          value={toTime(s.schedule_hour, s.schedule_minute)}
          onChange={(e) => {
            const v = fromTime(e.target.value);
            if (v)
              void svc.updateSettings({
                schedule_hour: v[0],
                schedule_minute: v[1],
              });
          }}
        />
      </SettingContainer>
      <ToggleSwitch
        checked={s.idle_trigger_enabled}
        onChange={(v) => void svc.updateSettings({ idle_trigger_enabled: v })}
        label={t("personal.verbatim.settings.idle.title")}
        description={t("personal.verbatim.settings.idle.description")}
        descriptionMode="tooltip"
        grouped
      />
      {s.idle_trigger_enabled && (
        <SettingContainer
          title={t("personal.verbatim.settings.idleMinutes.title")}
          description={t("personal.verbatim.settings.idleMinutes.description")}
          grouped
        >
          <Input
            type="number"
            variant="compact"
            className="w-20"
            min={5}
            max={240}
            value={s.idle_minutes}
            onChange={(e) =>
              void svc.updateSettings({
                idle_minutes: Number(e.target.value) || 20,
              })
            }
          />
        </SettingContainer>
      )}
      <ToggleSwitch
        checked={s.skip_on_battery}
        onChange={(v) => void svc.updateSettings({ skip_on_battery: v })}
        label={t("personal.verbatim.settings.battery.title")}
        description={t("personal.verbatim.settings.battery.description")}
        descriptionMode="tooltip"
        grouped
      />
      <ToggleSwitch
        checked={s.time_budget_enabled}
        onChange={(v) => void svc.updateSettings({ time_budget_enabled: v })}
        label={t("personal.verbatim.settings.budget.title")}
        description={t("personal.verbatim.settings.budget.description")}
        descriptionMode="tooltip"
        grouped
      />
      {s.time_budget_enabled && (
        <SettingContainer
          title={t("personal.verbatim.settings.stopBy.title")}
          description={t("personal.verbatim.settings.stopBy.description")}
          grouped
        >
          <Input
            type="time"
            variant="compact"
            value={toTime(s.stop_by_hour, s.stop_by_minute)}
            onChange={(e) => {
              const v = fromTime(e.target.value);
              if (v)
                void svc.updateSettings({
                  stop_by_hour: v[0],
                  stop_by_minute: v[1],
                });
            }}
          />
        </SettingContainer>
      )}
      <ToggleSwitch
        checked={s.auto_update}
        onChange={(v) => void svc.updateSettings({ auto_update: v })}
        label={t("personal.verbatim.settings.autoUpdate.title")}
        description={t("personal.verbatim.settings.autoUpdate.description")}
        descriptionMode="tooltip"
        grouped
      />

      <ToggleSwitch
        checked={s.retention.enabled}
        onChange={(enabled) => {
          if (enabled) void askRetention(s.retention);
          else
            void svc.updateSettings({
              retention: { ...s.retention, enabled: false },
            });
        }}
        label={t("personal.verbatim.settings.retention.title")}
        description={t("personal.verbatim.settings.retention.description")}
        descriptionMode="tooltip"
        grouped
      />
      <SettingContainer
        title={t("personal.verbatim.settings.retentionDays.title")}
        description={t("personal.verbatim.settings.retentionDays.description")}
        grouped
      >
        <div className="flex items-center gap-2 text-xs">
          <span>{t("personal.verbatim.settings.retentionDays.compress")}</span>
          <Input
            type="number"
            variant="compact"
            className="w-16"
            min={0}
            value={s.retention.compress_after_days}
            onChange={(e) =>
              void svc.updateSettings({
                retention: {
                  ...s.retention,
                  compress_after_days: Number(e.target.value) || 0,
                },
              })
            }
          />
          <span>{t("personal.verbatim.settings.retentionDays.delete")}</span>
          <Input
            type="number"
            variant="compact"
            className="w-16"
            min={0}
            value={s.retention.delete_after_days}
            onChange={(e) =>
              void svc.updateSettings({
                retention: {
                  ...s.retention,
                  delete_after_days: Number(e.target.value) || 0,
                },
              })
            }
          />
          <Button
            size="sm"
            variant="ghost"
            onClick={() => void askRetention(s.retention)}
          >
            {t("personal.verbatim.settings.retention.preview")}
          </Button>
        </div>
      </SettingContainer>

      <ToggleSwitch
        checked={s.coaching_weekly}
        onChange={(v) => void svc.updateSettings({ coaching_weekly: v })}
        label={t("personal.verbatim.settings.coaching.title")}
        description={t("personal.verbatim.settings.coaching.description")}
        descriptionMode="tooltip"
        grouped
      />
      {s.coaching_weekly && providers.length > 0 && (
        <SettingContainer
          title={t("personal.verbatim.settings.coachingProvider.title")}
          description={t(
            "personal.verbatim.settings.coachingProvider.description",
          )}
          grouped
        >
          <Dropdown
            options={providers}
            selectedValue={s.coaching_provider_id}
            onSelect={(v) =>
              void svc.updateSettings({ coaching_provider_id: v })
            }
          />
        </SettingContainer>
      )}

      <SettingContainer
        title={t("personal.verbatim.settings.paths.title")}
        description={t("personal.verbatim.settings.paths.description")}
        grouped
        layout="stacked"
      >
        <div className="grid grid-cols-1 gap-2">
          <Input
            variant="compact"
            placeholder={status?.worker_dir ?? ""}
            value={workerDir}
            onChange={(e) => setWorkerDir(e.target.value)}
            onBlur={() => {
              if (workerDir !== s.worker_dir)
                void svc.updateSettings({ worker_dir: workerDir.trim() });
            }}
          />
          <Input
            variant="compact"
            placeholder={
              status?.uv_path ?? t("personal.verbatim.settings.paths.uvAuto")
            }
            value={uvPath}
            onChange={(e) => setUvPath(e.target.value)}
            onBlur={() => {
              if (uvPath !== s.uv_path)
                void svc.updateSettings({ uv_path: uvPath.trim() });
            }}
          />
        </div>
      </SettingContainer>

      <Dialog
        open={preview !== null}
        onOpenChange={(open) => {
          if (!open) {
            setPreview(null);
            setPendingRetention(null);
          }
        }}
        title={t("personal.verbatim.retentionPreview.title")}
        description={t("personal.verbatim.retentionPreview.description")}
        closeLabel={t("personal.verbatim.retentionPreview.cancel")}
        footer={
          <div className="flex justify-end gap-2">
            <Button
              variant="secondary"
              onClick={() => {
                setPreview(null);
                setPendingRetention(null);
              }}
            >
              {t("personal.verbatim.retentionPreview.cancel")}
            </Button>
            {pendingRetention && !s.retention.enabled && (
              <Button
                variant="primary"
                onClick={() => {
                  void svc.updateSettings({
                    retention: { ...pendingRetention, enabled: true },
                  });
                  setPreview(null);
                  setPendingRetention(null);
                }}
              >
                {t("personal.verbatim.retentionPreview.enable")}
              </Button>
            )}
          </div>
        }
      >
        {preview && (
          <div className="space-y-2 text-sm">
            <p>
              {t("personal.verbatim.retentionPreview.compress", {
                count: preview.compress.length,
                size: formatBytes(preview.bytes_to_compress),
                saved: formatBytes(preview.estimated_bytes_saved),
              })}
            </p>
            <p>
              {t("personal.verbatim.retentionPreview.delete", {
                count: preview.delete.length,
                size: formatBytes(preview.bytes_to_delete),
              })}
            </p>
            <p className="text-ink-3 text-xs">
              {t("personal.verbatim.retentionPreview.kept", {
                unprocessed: preview.kept_unprocessed,
                unverified: preview.kept_unverified,
                history: preview.kept_in_history,
              })}
            </p>
            {preview.delete.length > 0 && (
              <ul className="text-xs text-ink-3 max-h-32 overflow-y-auto">
                {preview.delete.slice(0, 50).map((i) => (
                  <li key={i.entry_id}>
                    {new Date(i.created_at * 1000).toLocaleDateString()} ·{" "}
                    {i.file}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </Dialog>
    </SettingsGroup>
  );
};
