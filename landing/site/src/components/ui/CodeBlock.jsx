import { useMemo } from "react";
import { highlight } from "../../lib/highlight";
import CopyButton from "./CopyButton";

export default function CodeBlock({ code, lang = "bash", className = "" }) {
  const html = useMemo(() => highlight(code, lang), [code, lang]);

  return (
    <div className={`code-block ${className}`}>
      <div className="code-block__bar">
        <span className="code-block__lang">{lang}</span>
        <CopyButton text={code} label="Copy code" copiedLabel="Copied" />
      </div>
      <pre className="code-block__pre">
        <code
          className={`hljs language-${lang}`}
          dangerouslySetInnerHTML={{ __html: html }}
        />
      </pre>
    </div>
  );
}
