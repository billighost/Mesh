import { useState } from "react";
import Icon from "./Icon";

export default function Accordion({ items, idBase = "faq", defaultOpen = 0 }) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <div className="accordion">
      {items.map((item, index) => {
        const isOpen = open === index;
        return (
          <div className={`accordion__item ${isOpen ? "is-open" : ""}`} key={item.q}>
            <h3 className="accordion__heading">
              <button
                type="button"
                className="accordion__trigger"
                id={`${idBase}-trigger-${index}`}
                aria-expanded={isOpen}
                aria-controls={`${idBase}-panel-${index}`}
                onClick={() => setOpen(isOpen ? -1 : index)}
              >
                <span className="accordion__index">{String(index + 1).padStart(2, "0")}</span>
                <span className="accordion__question">{item.q}</span>
                <Icon name="chevron" size={18} className="accordion__chevron" />
              </button>
            </h3>
            <div
              className="accordion__panel"
              id={`${idBase}-panel-${index}`}
              role="region"
              aria-labelledby={`${idBase}-trigger-${index}`}
              hidden={!isOpen}
            >
              <p>{item.a}</p>
            </div>
          </div>
        );
      })}
    </div>
  );
}
