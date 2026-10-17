import { useEffect, useRef } from "react";
import { usePreferences } from "../../hooks/usePreferences";

/**
 * A soft light that follows a fine pointer across the page. It is painted
 * above content but blends with it, so text stays crisp. Disabled entirely
 * when motion is off.
 */
export default function CursorSpotlight() {
  const ref = useRef(null);
  const { motion } = usePreferences();

  useEffect(() => {
    if (!motion) return;
    if (typeof window.matchMedia !== "function") return;
    if (!window.matchMedia("(pointer: fine)").matches) return;

    let frame = 0;
    const onMove = (event) => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const el = ref.current;
        if (!el) return;
        el.style.setProperty("--sx", `${event.clientX}px`);
        el.style.setProperty("--sy", `${event.clientY}px`);
        el.classList.add("is-live");
      });
    };
    const onLeave = () => {
      ref.current?.classList.remove("is-live");
    };

    window.addEventListener("pointermove", onMove, { passive: true });
    document.addEventListener("mouseleave", onLeave);
    return () => {
      window.removeEventListener("pointermove", onMove);
      document.removeEventListener("mouseleave", onLeave);
      cancelAnimationFrame(frame);
    };
  }, [motion]);

  if (!motion) return null;
  return <div ref={ref} className="cursor-spotlight" aria-hidden="true" />;
}
