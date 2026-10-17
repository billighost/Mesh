import { useEffect, useRef, useState } from "react";
import { usePreferences } from "../../hooks/usePreferences";
import Icon from "./Icon";
import Magnetic from "./Magnetic";

export default function Button({
  children,
  variant = "primary",
  size,
  href,
  icon,
  iconRight,
  className = "",
  onPointerDown,
  ...props
}) {
  const { motion } = usePreferences();
  const [pressed, setPressed] = useState(false);
  const pressTimer = useRef(0);

  useEffect(() => () => clearTimeout(pressTimer.current), []);

  const handlePointerDown = (event) => {
    if (motion && event.pointerType !== "touch") {
      clearTimeout(pressTimer.current);
      setPressed(false);
      requestAnimationFrame(() => setPressed(true));
      pressTimer.current = setTimeout(() => setPressed(false), 360);
    }
    onPointerDown?.(event);
  };

  const classes = [
    "btn",
    `btn--${variant}`,
    size ? `btn--${size}` : "",
    pressed ? "is-pressed" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  const content = (
    <>
      {icon && <Icon name={icon} size={16} />}
      <span>{children}</span>
      {iconRight && <Icon name={iconRight} size={16} />}
    </>
  );

  const element = href ? (
    <a
      className={classes}
      href={href}
      onPointerDown={handlePointerDown}
      {...(/^https?:/.test(href) ? { target: "_blank", rel: "noopener noreferrer" } : {})}
      {...props}
    >
      {content}
    </a>
  ) : (
    <button type="button" className={classes} onPointerDown={handlePointerDown} {...props}>
      {content}
    </button>
  );

  return (
    <Magnetic className={className.includes("btn--block") ? "magnetic--block" : ""}>
      {element}
    </Magnetic>
  );
}
