// Fork-only ("Handy Personal") UI strings. Merged into the English
// `translation` bundle at runtime so upstream's locale files (and the
// translation completeness check) stay untouched; other languages fall back
// to these English strings via i18next's fallbackLng.
import i18n from "@/i18n";
import personalEn from "./personal.en.json";

let registered = false;

export const registerPersonalTranslations = () => {
  if (registered) return;
  registered = true;
  i18n.addResourceBundle("en", "translation", { personal: personalEn }, true);
};

registerPersonalTranslations();
