import { usePreferences } from "../../hooks/usePreferences";

export default function Marquee({ items, label }) {
  const { motion } = usePreferences();
  const content = (hidden) => (
    <span className="marquee__group" aria-hidden={hidden || undefined}>
      {items.map((item) => (
        <span className="marquee__item" key={item}>
          {item}
          <span className="marquee__dot" aria-hidden="true">·</span>
        </span>
      ))}
    </span>
  );

  return (
    <div className={`marquee ${motion ? "" : "is-static"}`} aria-label={label}>
      {!motion ? (
        <div className="marquee__static">{items.join(" · ")} · +18 more</div>
      ) : (
        <div className="marquee__track">
          {content(false)}
          {content(true)}
        </div>
      )}
    </div>
  );
}
