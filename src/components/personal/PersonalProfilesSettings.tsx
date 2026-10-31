import React, { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { commands, type AppProfile, type ProfileLlm } from "@/bindings";
import { useSettings } from "@/hooks/useSettings";
import { Input } from "../ui/Input";
import { Button } from "../ui/Button";
import { Switch } from "../ui/Switch";
import { SettingsGroup } from "../ui/SettingsGroup";
import { usePersonalSettings } from "./usePersonalSettings";
import "./i18n";

const selectClass =
  "px-2 py-1 text-xs bg-wash border border-line-strong rounded-md hover:border-logo-primary focus:outline-none focus:border-logo-primary";

const LLM_MODES: ProfileLlm[] = ["default", "off", "always"];

const emptyProfile = (): AppProfile => ({
  name: "",
  bundle_id: "",
  llm: "default",
  prompt_id: null,
  auto_submit: null,
  enabled: true,
});

/** Per-app profiles keyed on the frontmost app's bundle id. */
export const PersonalProfilesSettings: React.FC = () => {
  const { t } = useTranslation();
  const { settings } = useSettings();
  const { personal, updatePersonal } = usePersonalSettings();
  const [recentIds, setRecentIds] = useState<string[]>([]);
  // Text fields are edited locally and saved on blur.
  const [draft, setDraft] = useState<AppProfile[]>([]);

  useEffect(() => {
    setDraft(personal?.app_profiles ?? []);
  }, [personal?.app_profiles]);

  useEffect(() => {
    commands.getRecentAppIds().then((r) => {
      if (r.status === "ok") setRecentIds(r.data);
    });
  }, []);

  if (!personal) return null;
  const prompts = settings?.post_process_prompts ?? [];

  const save = (profiles: AppProfile[]) => {
    setDraft(profiles);
    void updatePersonal({ app_profiles: profiles });
  };
  const patch = (i: number, p: Partial<AppProfile>, persist = true) => {
    const next = draft.map((x, k) => (k === i ? { ...x, ...p } : x));
    if (persist) save(next);
    else setDraft(next);
  };

  return (
    <SettingsGroup
      title={t("personal.profiles.title")}
      description={t("personal.profiles.description")}
    >
      <datalist id="personal-recent-app-ids">
        {recentIds.map((id) => (
          <option key={id} value={id} />
        ))}
      </datalist>
      {draft.map((p, i) => (
        <div key={i} className="grid gap-1.5 px-4 py-2 text-xs">
          <div className="flex items-center gap-2">
            <Switch
              checked={p.enabled}
              onChange={(enabled) => patch(i, { enabled })}
            />
            <Input
              variant="compact"
              className="w-32"
              placeholder={t("personal.profiles.name")}
              value={p.name}
              onChange={(e) => patch(i, { name: e.target.value }, false)}
              onBlur={() => save(draft)}
            />
            <Input
              variant="compact"
              className="flex-1 min-w-0 font-mono font-normal"
              placeholder="com.example.app"
              list="personal-recent-app-ids"
              value={p.bundle_id}
              onChange={(e) => patch(i, { bundle_id: e.target.value }, false)}
              onBlur={() => save(draft)}
            />
            <Button
              variant="secondary"
              size="sm"
              onClick={() => save(draft.filter((_, k) => k !== i))}
            >
              {t("personal.profiles.remove")}
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-2 ps-12">
            <select
              className={selectClass}
              aria-label={t("personal.profiles.llm")}
              value={p.llm}
              onChange={(e) => patch(i, { llm: e.target.value as ProfileLlm })}
            >
              {LLM_MODES.map((m) => (
                <option key={m} value={m}>
                  {t(`personal.profiles.llmModes.${m}`)}
                </option>
              ))}
            </select>
            <select
              className={selectClass}
              aria-label={t("personal.profiles.prompt")}
              value={p.prompt_id ?? ""}
              disabled={p.llm === "off"}
              onChange={(e) => patch(i, { prompt_id: e.target.value || null })}
            >
              <option value="">{t("personal.profiles.selectedPrompt")}</option>
              {prompts.map((pr) => (
                <option key={pr.id} value={pr.id}>
                  {pr.name}
                </option>
              ))}
            </select>
            <select
              className={selectClass}
              aria-label={t("personal.profiles.autoSubmit")}
              value={p.auto_submit === null ? "" : p.auto_submit ? "on" : "off"}
              onChange={(e) =>
                patch(i, {
                  auto_submit:
                    e.target.value === "" ? null : e.target.value === "on",
                })
              }
            >
              <option value="">
                {t("personal.profiles.autoSubmitDefault")}
              </option>
              <option value="on">{t("personal.profiles.autoSubmitOn")}</option>
              <option value="off">
                {t("personal.profiles.autoSubmitOff")}
              </option>
            </select>
          </div>
        </div>
      ))}
      <div className="px-4 py-2">
        <Button
          variant="secondary"
          size="sm"
          onClick={() => setDraft([...draft, emptyProfile()])}
        >
          {t("personal.profiles.add")}
        </Button>
      </div>
    </SettingsGroup>
  );
};
