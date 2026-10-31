import React, { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { SettingContainer } from "../ui/SettingContainer";
import { Input } from "../ui/Input";
import { usePersonalSettings } from "./usePersonalSettings";
import "./i18n";

const MIN_TIMEOUT_MS = 250;
const MAX_TIMEOUT_MS = 60_000;

/** LLM timeout: past it the rule-cleaned text is pasted instead. */
export const PersonalLlmSettings: React.FC = () => {
  const { t } = useTranslation();
  const { personal, updatePersonal } = usePersonalSettings();
  const [seconds, setSeconds] = useState("");

  const storedMs = personal?.llm_timeout_ms;
  useEffect(() => {
    if (storedMs !== undefined) setSeconds(String(storedMs / 1000));
  }, [storedMs]);

  if (!personal) return null;

  const commit = () => {
    const parsed = Number.parseFloat(seconds);
    if (!Number.isFinite(parsed)) {
      setSeconds(String(personal.llm_timeout_ms / 1000));
      return;
    }
    const ms = Math.min(
      MAX_TIMEOUT_MS,
      Math.max(MIN_TIMEOUT_MS, Math.round(parsed * 1000)),
    );
    setSeconds(String(ms / 1000));
    if (ms !== personal.llm_timeout_ms) {
      void updatePersonal({ llm_timeout_ms: ms });
    }
  };

  return (
    <SettingContainer
      title={t("personal.settings.llmTimeout.title")}
      description={t("personal.settings.llmTimeout.description")}
      descriptionMode="tooltip"
      layout="horizontal"
      grouped={true}
    >
      <div className="flex items-center gap-2">
        <Input
          type="number"
          variant="compact"
          min={MIN_TIMEOUT_MS / 1000}
          max={MAX_TIMEOUT_MS / 1000}
          step={0.5}
          className="w-20"
          value={seconds}
          onChange={(e) => setSeconds(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
          }}
        />
        <span className="text-sm text-ink-3">
          {t("personal.settings.llmTimeout.unit")}
        </span>
      </div>
    </SettingContainer>
  );
};
