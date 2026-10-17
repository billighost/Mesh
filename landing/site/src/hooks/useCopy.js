import { useCallback, useRef, useState } from "react";
import { useToast } from "../components/ui/Toast";

export function useCopy() {
  const showToast = useToast();
  const [copied, setCopied] = useState(false);
  const timer = useRef(null);

  const copy = useCallback(
    async (text, label = "Copied to clipboard") => {
      let ok = false;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
          ok = true;
        }
      } catch {}

      if (!ok) {
        try {
          const area = document.createElement("textarea");
          area.value = text;
          area.setAttribute("readonly", "");
          area.style.position = "fixed";
          area.style.opacity = "0";
          document.body.appendChild(area);
          area.select();
          ok = document.execCommand("copy");
          document.body.removeChild(area);
        } catch {
          ok = false;
        }
      }

      if (ok) {
        setCopied(true);
        showToast(label);
        clearTimeout(timer.current);
        timer.current = setTimeout(() => setCopied(false), 1800);
      } else {
        showToast("Press Ctrl/Cmd+C to copy");
      }
      return ok;
    },
    [showToast]
  );

  return [copied, copy];
}
