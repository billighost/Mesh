import { useInView } from "../../hooks/useInView";
import { usePreferences } from "../../hooks/usePreferences";

export default function Reveal({ children, delay = 0, className = "", as: Tag = "div" }) {
  const [ref, inView] = useInView();
  const { motion } = usePreferences();
  const shown = !motion || inView;

  return (
    <Tag
      ref={ref}
      className={`reveal ${shown ? "is-in" : ""} ${className}`}
      style={{ "--reveal-delay": `${delay}ms` }}
    >
      {children}
    </Tag>
  );
}
