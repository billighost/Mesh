import { useCopy } from "../../hooks/useCopy";
import Icon from "./Icon";

export default function CopyButton({ text, label = "Copy", copiedLabel = "Copied", compact = false, className = "" }) {
  const [copied, copy] = useCopy();
  return (
    <button
      type="button"
      className={`copy-btn ${compact ? "copy-btn--compact" : ""} ${className}`}
      onClick={() => copy(text, copied ? "Copied again" : copiedLabel)}
      aria-label={copied ? copiedLabel : label}
    >
      <Icon name={copied ? "check" : "copy"} size={14} />
      {!compact && <span>{copied ? copiedLabel : label}</span>}
    </button>
  );
}
