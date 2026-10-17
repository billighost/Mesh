import { useCallback, useEffect, useRef, useState } from "react";
import { INSTALL_CMD, GITHUB_URL, AGENSI_URL, nav } from "../data/content";
import { usePreferences } from "../hooks/usePreferences";
import { useCopy } from "../hooks/useCopy";
import Button from "./ui/Button";
import Icon from "./ui/Icon";
import Mark from "./ui/Mark";
import Segmented from "./ui/Segmented";
import Switch from "./ui/Switch";

const themeOptions = [
  { value: "system", label: "System" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

export default function Topbar() {
  const { themePref, setThemePref, motion, setMotion } = usePreferences();
  const [, copy] = useCopy();
  const [open, setOpen] = useState(false);
  const [closing, setClosing] = useState(false);
  const panelRef = useRef(null);
  const triggerRef = useRef(null);
  const closeTimer = useRef(0);

  const closeMenu = useCallback(() => {
    clearTimeout(closeTimer.current);
    setClosing(true);
    closeTimer.current = setTimeout(
      () => {
        setOpen(false);
        setClosing(false);
      },
      motion ? 200 : 0
    );
    triggerRef.current?.focus();
  }, [motion]);

  const openMenu = () => {
    clearTimeout(closeTimer.current);
    setClosing(false);
    setOpen(true);
  };

  useEffect(() => () => clearTimeout(closeTimer.current), []);

  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const panel = panelRef.current;
    const focusables = panel
      ? Array.from(panel.querySelectorAll("a[href], button:not([disabled])")).filter(
          (node) => node.offsetParent !== null
        )
      : [];
    focusables[0]?.focus();

    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        closeMenu();
        return;
      }
      if (event.key !== "Tab" || focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open, closeMenu]);

  return (
    <header className="topbar">
      <div className="wrap topbar__inner">
        <a className="brand" href="#top" aria-label="Mesh home">
          <Mark size={22} />
          <span className="brand__word">mesh</span>
          <span className="brand__version">v0.6.3</span>
        </a>

        <nav className="nav" aria-label="Primary">
          {nav.map((item) => (
            <a className="nav__link" href={item.href} key={item.href}>
              {item.label}
            </a>
          ))}
        </nav>

        <div className="topbar__controls">
          <Segmented
            options={themeOptions}
            value={themePref}
            onChange={setThemePref}
            label="Color theme"
          />
          <Switch checked={motion} onChange={setMotion} label="Motion" />
          <Button
            variant="primary"
            className="topbar__install"
            icon="copy"
            onClick={() => copy(INSTALL_CMD, "Install command copied")}
          >
            Install
          </Button>
          <button
            ref={triggerRef}
            type="button"
            className="menu-btn"
            aria-label={open ? "Close menu" : "Open menu"}
            aria-expanded={open}
            aria-controls="mobile-menu"
            onClick={() => (open ? closeMenu() : openMenu())}
          >
            <Icon name={open ? "close" : "menu"} size={20} />
          </button>
        </div>
      </div>

      {open && (
        <div
          className={`mobile-menu ${closing ? "is-closing" : ""}`}
          id="mobile-menu"
          ref={panelRef}
        >
          <nav className="mobile-menu__nav" aria-label="Mobile">
            {nav.map((item) => (
              <a
                className="mobile-menu__link"
                href={item.href}
                key={item.href}
                onClick={closeMenu}
              >
                {item.label}
                <span>↗</span>
              </a>
            ))}
          </nav>
          <div className="mobile-menu__controls">
            <div className="mobile-menu__row">
              <span className="mobile-menu__label">Theme</span>
              <Segmented
                options={themeOptions}
                value={themePref}
                onChange={setThemePref}
                label="Color theme"
              />
            </div>
            <div className="mobile-menu__row">
              <span className="mobile-menu__label">Motion</span>
              <Switch checked={motion} onChange={setMotion} label={motion ? "On" : "Off"} />
            </div>
            <Button
              variant="primary"
              className="btn--block"
              icon="copy"
              onClick={() => copy(INSTALL_CMD, "Install command copied")}
            >
              Install mesh
            </Button>
            <div className="mobile-menu__row">
              <a className="btn btn--quiet" href={GITHUB_URL} target="_blank" rel="noopener noreferrer">
                GitHub <Icon name="arrowUpRight" size={14} />
              </a>
              <a className="btn btn--quiet" href={AGENSI_URL} target="_blank" rel="noopener noreferrer">
                Agensi <Icon name="arrowUpRight" size={14} />
              </a>
            </div>
          </div>
        </div>
      )}
    </header>
  );
}
