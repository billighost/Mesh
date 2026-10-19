// ─── Mesh Brand Tokens (Light Theme Variant) ──────────────────────────────────
// Sourced from landing/assets/brand/README.md and landing/site/src/styles/tokens.css

export const colors = {
  // Backgrounds
  bg: "#F7F8F3",
  surface: "#FFFFFF",
  surface2: "#EEF0E7",

  // Text
  ink: "#0B0D08",
  muted: "#5D6455",

  // Borders / lines
  line: "#D7DBCD",

  // Accent — lime/chartreuse
  accent: "#B9F24A",
  accentInk: "#0B0D08",  // text ON accent fill
  accentText: "#2F6B00",
  accentSoft: "#E8F9C7",

  // Warn / conflict
  warn: "#B45309",
  warnSoft: "#FEF3C7",

  // Terminal-specific (clean light-theme terminal)
  terminalBg: "#FFFFFF",
  terminalHeaderBg: "#EEF0E7",
  terminalInk: "#0B0D08",
  terminalDim: "#5D6455",
  terminalLine: "#D7DBCD",
} as const;

export const fonts = {
  display: "'Martian Mono', ui-monospace, monospace",
  body: "'Instrument Sans', 'Segoe UI', system-ui, sans-serif",
  mono: "'JetBrains Mono', ui-monospace, monospace",
} as const;

export const radius = {
  sm: 2,
  md: 6,
} as const;

// Hard neo-brutalist offset shadow in ink (#0B0D08)
export const shadow = `4px 4px 0px ${colors.ink}`;
export const shadowAccent = `4px 4px 0px ${colors.accent}`;

// Duration helpers (all in frames at 60fps)
export const FPS = 60;
export const sec = (s: number) => Math.round(s * FPS);

// Easing strings (for CSS transitions inside Remotion)
export const ease = "cubic-bezier(.2, .8, .2, 1)";
export const easeOut = "cubic-bezier(.16, 1, .3, 1)";

// The 3-node Mesh mark geometry (64×64 viewBox)
export const markNodes = {
  top:         { cx: 32, cy: 13, r: 7 },
  bottomLeft:  { cx: 12, cy: 50, r: 7 },
  bottomRight: { cx: 52, cy: 50, r: 7 },
} as const;

export const markPaths = {
  diagLeft:  "M27.6 21.1 L16.4 41.9",
  diagRight: "M36.4 21.1 L47.6 41.9",
  baseline:  "M19.0 50.0 L45.0 50.0",
} as const;
