// ─── Mesh Brand Tokens ────────────────────────────────────────────────────────
// Sourced from landing/assets/brand/README.md and landing/site/src/styles/tokens.css
// Video always uses dark-mode values.

export const colors = {
  // Backgrounds
  bg: "#0B0D08",
  surface: "#11140C",
  surface2: "#171B11",

  // Text
  ink: "#F4F7EC",
  muted: "#A2AA96",

  // Borders / lines
  line: "#2A2F22",

  // Accent — lime/chartreuse
  accent: "#C8F04A",
  accentInk: "#0B0D08",  // text ON accent fill
  accentSoft: "#1E2A0E",

  // Warn / conflict
  warn: "#FBBF24",
  warnSoft: "#2A230B",

  // Terminal-specific
  terminalBg: "#11140C",
  terminalInk: "#F4F7EC",
  terminalDim: "#A2AA96",
  terminalLine: "#2A2F22",
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

// Hard neo-brutalist offset shadow
export const shadow = `4px 4px 0px ${colors.bg}`;
export const shadowAccent = `4px 4px 0px ${colors.accentInk}`;

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
