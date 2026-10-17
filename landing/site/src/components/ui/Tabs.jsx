import { useRef } from "react";

export function TabList({ items, value, onChange, idBase, label }) {
  const refs = useRef([]);

  const move = (event, index) => {
    const keys = ["ArrowRight", "ArrowLeft", "Home", "End"];
    if (!keys.includes(event.key)) return;
    event.preventDefault();
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % items.length;
    if (event.key === "ArrowLeft") next = (index - 1 + items.length) % items.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = items.length - 1;
    onChange(items[next].id);
    refs.current[next]?.focus();
  };

  return (
    <div className="tablist" role="tablist" aria-label={label}>
      {items.map((item, index) => {
        const selected = item.id === value;
        return (
          <button
            key={item.id}
            ref={(node) => { refs.current[index] = node; }}
            type="button"
            role="tab"
            id={`${idBase}-tab-${item.id}`}
            aria-controls={`${idBase}-panel-${item.id}`}
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            className={`tab ${selected ? "is-active" : ""}`}
            onClick={() => onChange(item.id)}
            onKeyDown={(event) => move(event, index)}
          >
            {item.label}
          </button>
        );
      })}
    </div>
  );
}

export function TabPanel({ id, idBase, children, className = "" }) {
  return (
    <div
      role="tabpanel"
      id={`${idBase}-panel-${id}`}
      aria-labelledby={`${idBase}-tab-${id}`}
      className={`tabpanel ${className}`}
      tabIndex={0}
    >
      {children}
    </div>
  );
}
