import Icon from "./Icon";

function renderLine(line) {
  if (line === "ok  _") {
    return (
      <>
        <span className="term-ok">ok</span>
        <span className="term-text">  </span>
        <span className="terminal__cursor" />
      </>
    );
  }
  if (line.startsWith("$ ")) {
    return (
      <>
        <span className="term-dim">$</span>
        <span className="term-text"> {line.slice(2)}</span>
      </>
    );
  }
  if (line.startsWith("ok  ")) {
    return (
      <>
        <span className="term-ok">ok</span>
        <span className="term-text">  {line.slice(4)}</span>
      </>
    );
  }
  return <span className="term-text">{line}</span>;
}

export default function Terminal({
  title = "mesh / checkout-service",
  lines = [],
  liveLabel = "live",
  replayKey = 0,
  onReplay,
  caption,
  className = "",
}) {
  return (
    <div className={`terminal ${className}`}>
      <div className="terminal__bar">
        <span className="terminal__dots" aria-hidden="true">
          <span className="terminal__dot is-accent" />
          <span className="terminal__dot" />
          <span className="terminal__dot" />
        </span>
        <span className="terminal__title">{title}</span>
        {onReplay && (
          <button
            type="button"
            className="terminal__replay"
            aria-label="Replay terminal session"
            onClick={onReplay}
          >
            <Icon name="replay" size={13} />
          </button>
        )}
        <span className="terminal__live">
          <span className="terminal__live-dot" aria-hidden="true" />
          {liveLabel}
        </span>
      </div>
      <div className="terminal__body" key={replayKey}>
        {lines.map((line, index) => (
          <div
            className="terminal__line"
            key={`${index}-${line}`}
            style={{ "--line-delay": `${index * 70}ms` }}
          >
            {renderLine(line)}
          </div>
        ))}
      </div>
      {caption && <div className="terminal__caption">{caption}</div>}
    </div>
  );
}
