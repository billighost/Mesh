import { useEffect, useRef } from "react";
import { usePreferences } from "../../hooks/usePreferences";

/**
 * The preview's magnetic pull: the wrapper leans toward the pointer, then
 * springs back when the pointer leaves. Pointer-fine only, and still when the
 * visitor has motion switched off.
 */
export default function Magnetic({ children, className = "", strength = 0.22 }) {
  const ref = useRef(null);
  const frame = useRef(0);
  const { motion } = usePreferences();

  useEffect(() => {
    if (!motion && ref.current) ref.current.style.transform = "";
  }, [motion]);

  const onPointerMove = (event) => {
    if (!motion || event.pointerType !== "mouse" || !ref.current) return;
    const rect = ref.current.getBoundingClientRect();
    const x = (event.clientX - rect.left - rect.width / 2) * strength;
    const y = (event.clientY - rect.top - rect.height / 2) * strength;
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => {
      if (ref.current) {
        ref.current.style.transform = `translate(${x.toFixed(2)}px, ${y.toFixed(2)}px)`;
      }
    });
  };

  const onPointerLeave = () => {
    cancelAnimationFrame(frame.current);
    if (ref.current) ref.current.style.transform = "";
  };

  return (
    <span
      ref={ref}
      className={`magnetic ${className}`.trim()}
      onPointerMove={onPointerMove}
      onPointerLeave={onPointerLeave}
    >
      {children}
    </span>
  );
}
