import React from "react";
import { useTranslation } from "react-i18next";
import { ToggleSwitch } from "../ui/ToggleSwitch";
import { useSettings } from "@/hooks/useSettings";
import { useOsType } from "@/hooks/useOsType";
import { usePersonalSettings } from "./usePersonalSettings";
import "./i18n";

interface Props {
  descriptionMode?: "inline" | "tooltip";
  grouped?: boolean;
}

/** Reliable paste (promoted out of the debug panel) + password-field guard. */
export const PersonalPasteSettings: React.FC<Props> = ({
  descriptionMode = "tooltip",
  grouped = true,
}) => {
  const { t } = useTranslation();
  const { getSetting, updateSetting, isUpdating } = useSettings();
  const { personal, updatePersonal } = usePersonalSettings();
  const osType = useOsType();
  const reliableSupported = osType === "macos" || osType === "windows";

  return (
    <>
      {reliableSupported && (
        <ToggleSwitch
          checked={getSetting("reliable_paste") ?? false}
          onChange={(enabled) => updateSetting("reliable_paste", enabled)}
          isUpdating={isUpdating("reliable_paste")}
          label={t("personal.settings.reliablePaste.title")}
          description={t("personal.settings.reliablePaste.description")}
          descriptionMode={descriptionMode}
          grouped={grouped}
        />
      )}
      {osType === "macos" && (
        <ToggleSwitch
          checked={personal?.block_paste_in_secure_fields ?? true}
          onChange={(enabled) =>
            updatePersonal({ block_paste_in_secure_fields: enabled })
          }
          label={t("personal.settings.blockSecureFields.title")}
          description={t("personal.settings.blockSecureFields.description")}
          descriptionMode={descriptionMode}
          grouped={grouped}
        />
      )}
    </>
  );
};
