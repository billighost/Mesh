export default function Mark({ size = 22, className = "" }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      stroke="currentColor"
      strokeWidth="4.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <circle cx="32" cy="13" r="7" />
      <circle cx="12" cy="50" r="7" />
      <circle cx="52" cy="50" r="7" />
      <path d="M27.6 21.1 16.4 41.9" />
      <path d="M36.4 21.1 47.6 41.9" />
      <path d="M19 50h26" />

      {/* The signal follows the three edges: top -> left/right -> base. */}
      <g className="mark__signal" aria-hidden="true">
        <circle className="mark__halo mark__halo--top" cx="32" cy="13" r="7" />
        <circle className="mark__halo mark__halo--left" cx="12" cy="50" r="7" />
        <circle className="mark__halo mark__halo--right" cx="52" cy="50" r="7" />
        <path className="mark__signal-line mark__signal-line--left" pathLength="1" d="M27.6 21.1 16.4 41.9" />
        <path className="mark__signal-line mark__signal-line--right" pathLength="1" d="M36.4 21.1 47.6 41.9" />
        <path className="mark__signal-line mark__signal-line--base" pathLength="1" d="M19 50h26" />
      </g>
    </svg>
  );
}
