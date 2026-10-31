import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  commands,
  events,
  type VerbatimServiceSettings,
  type VerbatimServiceStatus,
} from "@/bindings";
import { useSettings } from "@/hooks/useSettings";

/** Status of the background verbatim worker, kept fresh by backend events. */
export const useVerbatimService = () => {
  const { settings, refreshSettings } = useSettings();
  const [status, setStatus] = useState<VerbatimServiceStatus | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (refresh: boolean) => {
    setLoading(true);
    const result = await commands.getVerbatimServiceStatus(refresh);
    setLoading(false);
    if (result.status === "ok") setStatus(result.data);
    else toast.error(result.error);
  }, []);

  useEffect(() => {
    void load(false);
    const unlisten = events.verbatimServiceChanged.listen(() => {
      void load(false);
    });
    return () => {
      void unlisten.then((u) => u());
    };
  }, [load]);

  const run = useCallback(
    async (
      action: () => Promise<{ status: "ok" | "error"; error?: string }>,
    ) => {
      const result = await action();
      if (result.status === "error" && result.error) toast.error(result.error);
      await load(true);
    },
    [load],
  );

  const updateSettings = useCallback(
    async (patch: Partial<VerbatimServiceSettings>) => {
      const current = settings?.personal?.verbatim_service;
      if (!current) return;
      const result = await commands.updateVerbatimServiceSettings({
        ...current,
        ...patch,
      });
      if (result.status === "error") toast.error(result.error);
      await refreshSettings();
      await load(false);
    },
    [settings, refreshSettings, load],
  );

  return {
    settings: settings?.personal?.verbatim_service ?? null,
    status,
    loading,
    refresh: () => load(true),
    updateSettings,
    runNow: () => run(() => commands.verbatimRunNow()),
    retry: (entryId: number | null) =>
      run(() => commands.verbatimRetry(entryId)),
    fix: () => run(() => commands.verbatimFix()),
    revealLogs: () => run(() => commands.verbatimRevealLogs()),
  };
};

export const formatBytes = (bytes: number | null | undefined) => {
  if (bytes === null || bytes === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
};

export const formatWhen = (unix: number | null | undefined) =>
  unix ? new Date(unix * 1000).toLocaleString() : "—";
