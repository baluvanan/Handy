import { useCallback } from "react";
import { toast } from "sonner";
import { commands, type PersonalSettings } from "@/bindings";
import { useSettings } from "@/hooks/useSettings";
import { useSettingsStore } from "@/stores/settingsStore";

/** Reads and updates the fork-only `settings.personal` block. */
export const usePersonalSettings = () => {
  const { settings, refreshSettings } = useSettings();
  const setSettings = useSettingsStore((s) => s.setSettings);
  const personal = settings?.personal ?? null;

  const updatePersonal = useCallback(
    async (patch: Partial<PersonalSettings>) => {
      const current = useSettingsStore.getState().settings;
      if (!current?.personal) return;
      const next: PersonalSettings = { ...current.personal, ...patch };
      // Optimistic update; the backend echoes what it stored.
      setSettings({ ...current, personal: next });
      const result = await commands.updatePersonalSettings(next);
      if (result.status === "error") {
        toast.error(result.error);
        await refreshSettings();
      }
    },
    [setSettings, refreshSettings],
  );

  return { personal, updatePersonal };
};
