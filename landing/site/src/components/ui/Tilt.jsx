import { useEffect, useRef } from "react";
import { usePreferences } from "../../hooks/usePreferences";

/** The preview's 3D tilt card — subtle, pointer-fine, motion-aware. */
export default function Tilt({ children, className = "", max = 7 }) {
  const ref = useRef(null);
  const frame = useRef(0);
  const { motion } = usePreferences();

  useEffect(() => {
    if (!motion && ref.current) ref.current.style.transform = "";
  }, [motion]);

  const onPointerMove = (event) => {
    if (!motion || event.pointerType !== "mouse" || !ref.current) return;
    const rect = ref.current.getBoundingClientRect();
    const rx = ((event.clientY - rect.top) / rect.height - 0.5) * -max;
    const ry = ((event.clientX - rect.left) / rect.width - 0.5) * max;
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => {
      if (ref.current) {
        ref.current.style.transform = `perspective(900px) rotateX(${rx.toFixed(2)}deg) rotateY(${ry.toFixed(2)}deg)`;
      }
    });
  };

  const onPointerLeave = () => {
    cancelAnimationFrame(frame.current);
    if (ref.current) ref.current.style.transform = "";
  };

  return (
    <div
      ref={ref}
      className={`tilt ${className}`.trim()}
      onPointerMove={onPointerMove}
      onPointerLeave={onPointerLeave}
    >
      {children}
    </div>
  );
}
