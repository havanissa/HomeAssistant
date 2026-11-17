import "./styles.css";

import { applyTheme } from "./prefs";
import { LiveConnection } from "./store";

import "./app-shell";

applyTheme();
new LiveConnection().start();
