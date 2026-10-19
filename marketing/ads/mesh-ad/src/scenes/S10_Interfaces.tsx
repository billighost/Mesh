import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts, radius } from "../lib/tokens";
import GridField from "../components/GridField";
import Kicker from "../components/Kicker";

const mcpTools = [
  "mesh_learn", "mesh_recall", "mesh_update", "mesh_forget", "mesh_inspect",
  "mesh_context", "mesh_patterns", "mesh_contradictions", "mesh_resolve_contradiction", "mesh_sync",
];

/**
 * Scene 10 — Interfaces Montage (frames 0–360)
 * Three-panel: CLI · MCP tools · Dashboard
 */
export default function S10_Interfaces() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const kickerOpacity = interpolate(frame, [0, 24], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Three panels stagger in
  const panelSprings = [0, 1, 2].map((i) =>
    spring({
      frame: frame - i * 18,
      fps,
      config: { damping: 22, stiffness: 120, mass: 0.7 },
    })
  );

  // Caption
  const captionOpacity = interpolate(frame, [260, 290], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // MCP tools scroll: one tool visible every 18 frames after panel 2 arrives
  const toolIndex = Math.min(
    mcpTools.length,
    Math.floor(interpolate(frame, [60, 60 + 18 * mcpTools.length], [0, mcpTools.length], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    }))
  );

  const panelStyle = (i: number): React.CSSProperties => ({
    opacity: interpolate(panelSprings[i], [0, 1], [0, 1]),
    transform: `translateY(${interpolate(panelSprings[i], [0, 1], [30, 0])}px)`,
    flex: 1,
    background: colors.surface,
    border: `1px solid ${colors.line}`,
    borderRadius: radius.sm,
    boxShadow: "4px 4px 0px #000",
    overflow: "hidden",
    fontFamily: fonts.mono,
    fontSize: 13,
  });

  return (
    <AbsoluteFill style={{ background: colors.bg }}>
      <GridField opacity={0.2} mask="center" />

      <div style={{ position: "absolute", top: 64, left: 80, opacity: kickerOpacity }}>
        <Kicker>CLI · MCP · Dashboard · REST API</Kicker>
      </div>

      {/* Three panels */}
      <AbsoluteFill
        style={{
          display: "flex",
          alignItems: "stretch",
          gap: 24,
          padding: "100px 60px 100px",
        }}
      >
        {/* Panel 1: CLI */}
        <div style={panelStyle(0)}>
          <div
            style={{
              padding: "12px 18px",
              borderBottom: `1px solid ${colors.terminalLine}`,
              fontSize: 16,
              fontWeight: 600,
              color: colors.muted,
              letterSpacing: "0.08em",
              background: colors.terminalBg,
            }}
          >
            CLI
          </div>
          <div
            style={{
              padding: "18px 20px",
              background: colors.terminalBg,
              color: colors.terminalDim,
              fontSize: 17,
              lineHeight: 1.85,
            }}
          >
            <div>
              <span style={{ color: colors.accent, fontWeight: 600 }}>$ </span>
              mesh add "postgres port 5432" --tags db,prod
            </div>
            <div style={{ color: colors.muted }}>ok  indexed / 0.08 ms</div>
            <div>
              <span style={{ color: colors.accent, fontWeight: 600 }}>$ </span>
              mesh ask "postgres port" --n 3
            </div>
            <div style={{ color: colors.accent, fontWeight: 600 }}>
              [0.93] postgres port 5432
            </div>
            <div>
              <span style={{ color: colors.accent, fontWeight: 600 }}>$ </span>
              mesh inspect --limit 10
            </div>
            <div style={{ color: colors.muted }}>10 memories · checkout-service▌</div>
          </div>
        </div>

        {/* Panel 2: MCP Tools */}
        <div style={panelStyle(1)}>
          <div
            style={{
              padding: "12px 18px",
              borderBottom: `1px solid ${colors.line}`,
              fontSize: 16,
              fontWeight: 600,
              color: colors.muted,
              letterSpacing: "0.08em",
            }}
          >
            10 MCP Tools
          </div>
          <div style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: 10 }}>
            {mcpTools.slice(0, toolIndex).map((tool, i) => (
              <div
                key={tool}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  fontSize: 16,
                  fontWeight: i === toolIndex - 1 ? 600 : 500,
                  color: i === toolIndex - 1 ? colors.accent : colors.ink,
                  opacity: i === toolIndex - 1 ? 1 : 0.8,
                }}
              >
                <span
                  style={{
                    width: 7,
                    height: 7,
                    borderRadius: "50%",
                    background: i === toolIndex - 1 ? colors.accent : colors.muted,
                    display: "inline-block",
                    flexShrink: 0,
                  }}
                />
                {tool}
              </div>
            ))}
          </div>
        </div>

        {/* Panel 3: Dashboard */}
        <div style={panelStyle(2)}>
          <div
            style={{
              padding: "12px 18px",
              borderBottom: `1px solid ${colors.line}`,
              fontSize: 16,
              fontWeight: 600,
              color: colors.muted,
              letterSpacing: "0.08em",
              display: "flex",
              alignItems: "center",
              gap: 10,
            }}
          >
            <svg width="16" height="16" viewBox="0 0 64 64" fill="none"
              stroke={colors.accent} strokeWidth="4.5" strokeLinecap="round">
              <path d="M27.6 21.1 L16.4 41.9" /><path d="M36.4 21.1 L47.6 41.9" />
              <path d="M19.0 50.0 L45.0 50.0" />
              <circle cx="32" cy="13" r="7" /><circle cx="12" cy="50" r="7" /><circle cx="52" cy="50" r="7" />
            </svg>
            mesh-dashboard · checkout-service
          </div>
          <div style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: 8 }}>
            {/* Fake search bar */}
            <div
              style={{
                border: `1px solid ${colors.line}`,
                borderRadius: radius.sm,
                padding: "10px 14px",
                fontSize: 16,
                color: colors.muted,
                marginBottom: 10,
              }}
            >
              / search memories…
            </div>
            {/* Fake audit log entries */}
            {["recall · backend-agent · 0.93", "learn · frontend-agent · indexed", "inspect · review-agent · 10 results", "sync · deploy-agent · summary stored"].map((entry, i) => (
              <div
                key={i}
                style={{
                  fontSize: 16,
                  color: i === 0 ? colors.accent : colors.muted,
                  fontWeight: i === 0 ? 600 : 400,
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  opacity: interpolate(frame, [80 + i * 20, 100 + i * 20], [0, 1], {
                    extrapolateLeft: "clamp",
                    extrapolateRight: "clamp",
                  }),
                }}
              >
                <span
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: "50%",
                    background: i === 0 ? colors.accent : colors.line,
                    display: "inline-block",
                  }}
                />
                {entry}
              </div>
            ))}
          </div>
        </div>
      </AbsoluteFill>

      {/* Caption */}
      <div
        style={{
          position: "absolute",
          bottom: 40,
          left: 0,
          right: 0,
          textAlign: "center",
          opacity: captionOpacity,
        }}
      >
        <span
          style={{
            fontFamily: fonts.display,
            fontWeight: 700,
            fontSize: 40,
            letterSpacing: "-0.03em",
            color: colors.ink,
          }}
        >
          CLI · MCP · Dashboard · REST API.{" "}
          <span style={{ color: colors.muted }}>Pick your interface.</span>
        </span>
      </div>
    </AbsoluteFill>
  );
}
