import "./styles.css";

import { applyTheme } from "./prefs";
import { live } from "./store";

import "./app-shell";

applyTheme();
live.start();
