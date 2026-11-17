// Per-browser display preferences (theme, advanced settings). Kept in
// localStorage, which can be unavailable (private windows, blocked storage):
// every access is guarded and the defaults always work.

export type ThemePref = "auto" | "light" | "dark";

const THEME_KEY = "smart-lighting.theme";
const ADVANCED_KEY = "smart-lighting.advanced";

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* storage unavailable: the choice lasts for this page only */
  }
}

let theme: ThemePref = ((): ThemePref => {
  const value = read(THEME_KEY);
  return value === "light" || value === "dark" ? value : "auto";
})();

let advanced = read(ADVANCED_KEY) === "1";

export function getTheme(): ThemePref {
  return theme;
}

export function setTheme(value: ThemePref): void {
  theme = value;
  write(THEME_KEY, value);
  applyTheme();
}

export function applyTheme(): void {
  const root = document.documentElement;
  if (theme === "auto") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export function getAdvanced(): boolean {
  return advanced;
}

export function setAdvanced(value: boolean): void {
  advanced = value;
  write(ADVANCED_KEY, value ? "1" : "0");
}
