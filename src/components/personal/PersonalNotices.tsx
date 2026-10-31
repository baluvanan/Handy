import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { events } from "@/bindings";
import "./i18n";

/** Renders backend `PersonalNotice` events as small, non-blocking toasts. */
export const PersonalNotices = () => {
  const { t } = useTranslation();

  useEffect(() => {
    const unlisten = events.personalNotice.listen((event) => {
      const { kind, detail } = event.payload;
      if (kind === "paste_blocked_secure_field") {
        toast.warning(t("personal.notices.pasteBlockedSecureField.title"), {
          description: t(
            "personal.notices.pasteBlockedSecureField.description",
          ),
        });
      } else if (kind === "llm_fallback") {
        toast.warning(t("personal.notices.llmFallback.title"), {
          description: detail
            ? `${t("personal.notices.llmFallback.description")} (${detail})`
            : t("personal.notices.llmFallback.description"),
        });
      }
    });
    return () => {
      unlisten.then((fn) => fn());
    };
  }, [t]);

  return null;
};
