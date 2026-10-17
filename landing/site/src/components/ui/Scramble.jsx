import { useEffect, useState } from "react";
import { useInView } from "../../hooks/useInView";
import { usePreferences } from "../../hooks/usePreferences";

const glyphs = "!<>-_\\/[]{}=+*^?#01";

/** The preview's text scramble, played once when the element enters view. */
export default function Scramble({ text, className = "" }) {
  const [ref, inView] = useInView({ threshold: 0.4 });
  const { motion } = usePreferences();
  const [out, setOut] = useState(text);

  useEffect(() => {
    if (!motion) {
      setOut(text);
      return;
    }
    if (!inView) return;
    let index = 0;
    const id = setInterval(() => {
      setOut(
        text
          .split("")
          .map((char, position) => {
            if (char === " ") return " ";
            return position < index
              ? char
              : glyphs[Math.floor(Math.random() * glyphs.length)];
          })
          .join("")
      );
      index += 1;
      if (index > text.length) clearInterval(id);
    }, 26);
    return () => clearInterval(id);
  }, [inView, motion, text]);

  return (
    <span ref={ref} className={className} aria-hidden="true">
      {out}
    </span>
  );
}
