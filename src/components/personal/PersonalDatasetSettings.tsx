import React, { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { ToggleSwitch } from "../ui/ToggleSwitch";
import { SettingContainer } from "../ui/SettingContainer";
import { Textarea } from "../ui/Textarea";
import { usePersonalSettings } from "./usePersonalSettings";
import "./i18n";

interface Props {
  descriptionMode?: "inline" | "tooltip";
  grouped?: boolean;
}

/** Dataset logging + verbatim transcription toggles. */
export const PersonalDatasetSettings: React.FC<Props> = ({
  descriptionMode = "tooltip",
  grouped = true,
}) => {
  const { t } = useTranslation();
  const { personal, updatePersonal } = usePersonalSettings();
  const [prompt, setPrompt] = useState(personal?.verbatim_prompt ?? "");

  useEffect(() => {
    setPrompt(personal?.verbatim_prompt ?? "");
  }, [personal?.verbatim_prompt]);

  if (!personal) return null;

  return (
    <>
      <ToggleSwitch
        checked={personal.dataset_logging_enabled}
        onChange={(enabled) =>
          updatePersonal({ dataset_logging_enabled: enabled })
        }
        label={t("personal.settings.datasetLogging.title")}
        description={t("personal.settings.datasetLogging.description")}
        descriptionMode={descriptionMode}
        grouped={grouped}
      />
      <ToggleSwitch
        checked={personal.verbatim_mode}
        onChange={(enabled) => updatePersonal({ verbatim_mode: enabled })}
        label={t("personal.settings.verbatim.title")}
        description={t("personal.settings.verbatim.description")}
        descriptionMode={descriptionMode}
        grouped={grouped}
      />
      {personal.verbatim_mode && (
        <SettingContainer
          title={t("personal.settings.verbatimPrompt.title")}
          description={t("personal.settings.verbatimPrompt.description")}
          descriptionMode={descriptionMode}
          grouped={grouped}
          layout="stacked"
        >
          <Textarea
            variant="compact"
            className="w-full"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onBlur={() => {
              if (prompt !== personal.verbatim_prompt) {
                updatePersonal({ verbatim_prompt: prompt });
              }
            }}
          />
        </SettingContainer>
      )}
    </>
  );
};
