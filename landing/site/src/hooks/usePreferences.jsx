import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

const PreferencesContext = createContext(null);

function safeRead(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function safeWrite(key, value) {
  try { localStorage.setItem(key, value); } catch {}
}

export function PreferencesProvider({ children }) {
  const [themePref, setThemePrefState] = useState(
    () => document.documentElement.dataset.themePref || "light"
  );
  const [motion, setMotionState] = useState(
    () => document.documentElement.dataset.motion !== "off"
  );
  const [systemDark, setSystemDark] = useState(
    () => window.matchMedia("(prefers-color-scheme: dark)").matches
  );

  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (event) => setSystemDark(event.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const resolvedTheme = themePref === "system" ? (systemDark ? "dark" : "light") : themePref;

  useEffect(() => {
    const root = document.documentElement;
    root.dataset.theme = resolvedTheme;
    root.dataset.themePref = themePref;
    root.style.colorScheme = resolvedTheme;
    safeWrite("mesh-theme", themePref);
  }, [resolvedTheme, themePref]);

  useEffect(() => {
    document.documentElement.dataset.motion = motion ? "on" : "off";
    safeWrite("mesh-motion", motion ? "on" : "off");
  }, [motion]);

  const setThemePref = useCallback((next) => {
    if (next === "light" || next === "dark" || next === "system") setThemePrefState(next);
  }, []);

  const setMotion = useCallback((next) => setMotionState(Boolean(next)), []);

  const value = useMemo(
    () => ({ themePref, resolvedTheme, setThemePref, motion, setMotion }),
    [themePref, resolvedTheme, setThemePref, motion, setMotion]
  );

  return <PreferencesContext.Provider value={value}>{children}</PreferencesContext.Provider>;
}

export function usePreferences() {
  const value = useContext(PreferencesContext);
  if (!value) throw new Error("usePreferences must be used inside PreferencesProvider");
  return value;
}
