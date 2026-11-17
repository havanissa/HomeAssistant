// Navigation items and hash routing. Hash routing keeps every URL inside
// the Ingress prefix, so a reload or a deep link never leaves the App.

export interface NavItem {
  key: string;
  title: string;
  icon: string;
}

export const NAV: NavItem[] = [
  { key: "dashboard", title: "Dashboard", icon: "view-dashboard" },
  { key: "rooms", title: "Rooms", icon: "floor-plan" },
  { key: "presence", title: "Presence", icon: "motion-sensor" },
  { key: "lighting", title: "Lighting", icon: "lightbulb-group" },
  { key: "moods", title: "Moods", icon: "palette" },
  { key: "switches", title: "Switches", icon: "light-switch" },
  { key: "curtains", title: "Curtains", icon: "curtains" },
  { key: "predictions", title: "Predictions", icon: "crystal-ball" },
  { key: "diagnostics", title: "Diagnostics", icon: "stethoscope" },
  { key: "settings", title: "Settings", icon: "cog" },
];

export const FOUNDATION = new Set(["dashboard", "diagnostics", "settings"]);

/** Phone bottom navigation; every module (these included) is under "More". */
export const BOTTOM_NAV = ["dashboard", "rooms", "lighting", "moods"];

export function routeFromHash(): string {
  const key = window.location.hash.replace(/^#\/?/, "").split(/[/?]/)[0];
  return NAV.some((item) => item.key === key) ? key : "dashboard";
}

export function navigate(key: string): void {
  if (routeFromHash() === key && window.location.hash) return;
  window.location.hash = `/${key}`;
}
