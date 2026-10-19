import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts, MOBILE, radius } from "../lib/tokens";
import GridField from "../components/GridField";
import Kicker from "../components/Kicker";

const mcpTools = [
  "mesh_learn",
  "mesh_recall",
  "mesh_update",
  "mesh_forget",
  "mesh_inspect",
  "mesh_context",
  "mesh_patterns",
  "mesh_contradictions",
  "mesh_resolve_contradiction",
  "mesh_sync",
];

/**
 * Scene 10 — Interfaces Montage (frames 0–360)
 * Three stacked full-width mobile panels: CLI • 10 MCP Tools • Web Dashboard
 */
export default function S10_Interfaces() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const kickerOpacity = interpolate(frame, [0, 24], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Three panels stagger in (synced with 3 whooshes at scene frames 5, 25, 45)
  const panelSprings = [0, 1, 2].map((i) =>
    spring({
      frame: frame - (5 + i * 20),
      fps,
      config: { damping: 22, stiffness: 120, mass: 0.7 },
    })
  );

  const captionOpacity = interpolate(frame, [250, 285], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const toolIndex = Math.min(
    mcpTools.length,
    Math.floor(
      interpolate(frame, [45, 45 + 16 * mcpTools.length], [0, mcpTools.length], {
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
      })
    )
  );

  const panelStyle = (i: number): React.CSSProperties => ({
    opacity: interpolate(panelSprings[i], [0, 1], [0, 1]),
    transform: `translateY(${interpolate(panelSprings[i], [0, 1], [28, 0])}px)`,
    width: "100%",
    background: colors.surface,
    border: `1.5px solid ${colors.line}`,
    borderRadius: radius.md,
    boxShadow: "6px 6px 0px #000",
    overflow: "hidden",
    fontFamily: fonts.mono,
  });

  return (
    <AbsoluteFill style={{ background: colors.bg }}>
      <GridField opacity={0.22} mask="center" />

      {/* Header */}
      <div
        style={{
          position: "absolute",
          top: MOBILE.safeTop,
          left: MOBILE.padX,
          right: MOBILE.padX,
          opacity: kickerOpacity,
        }}
      >
        <Kicker>CLI • MCP • Dashboard • Python SDK</Kicker>
      </div>

      {/* Three Stacked Mobile Panels */}
      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "column",
          justifyContent: "flex-start",
          padding: `${MOBILE.safeTop + 76}px ${MOBILE.padX}px ${MOBILE.safeBottom}px`,
          gap: 24,
        }}
      >
        {/* Panel 1: CLI */}
        <div style={panelStyle(0)}>
          <div
            style={{
              padding: "14px 24px",
              borderBottom: `1.5px solid ${colors.terminalLine}`,
              fontSize: 19,
              fontWeight: 700,
              color: colors.accent,
              letterSpacing: "0.08em",
              background: colors.surface2,
              display: "flex",
              justifyContent: "space-between",
            }}
          >
            <span>01 / CLI &amp; REPL</span>
            <span style={{ color: colors.muted }}>mesh ask • mesh why</span>
          </div>
          <div
            style={{
              padding: "20px 26px",
              background: colors.terminalBg,
              color: colors.terminalInk,
              fontSize: 21,
              lineHeight: 1.65,
            }}
          >
            <div>
              <span style={{ color: colors.accent, fontWeight: 700 }}>$ </span>
              mesh add &quot;postgres port 5432&quot; --tags db,prod
            </div>
            <div>
              <span style={{ color: colors.accent, fontWeight: 700 }}>$ </span>
              mesh ask &quot;postgres port&quot; --n 3{" "}
              <span style={{ color: colors.accent, fontWeight: 700 }}>
                → [0.93] port 5432
              </span>
            </div>
          </div>
        </div>

        {/* Panel 2: 10 MCP Tools (2-column grid for mobile legibility) */}
        <div style={panelStyle(1)}>
          <div
            style={{
              padding: "14px 24px",
              borderBottom: `1.5px solid ${colors.line}`,
              fontSize: 19,
              fontWeight: 700,
              color: colors.accent,
              letterSpacing: "0.08em",
              background: colors.surface2,
              display: "flex",
              justifyContent: "space-between",
            }}
          >
            <span>02 / 10 STDIO MCP TOOLS</span>
            <span style={{ color: colors.muted }}>mesh-server</span>
          </div>
          <div
            style={{
              padding: "20px 26px",
              display: "grid",
              gridTemplateColumns: "1fr 1fr",
              gap: "12px 20px",
            }}
          >
            {mcpTools.map((tool, i) => {
              const visible = i < toolIndex;
              const isLatest = i === toolIndex - 1;
              return (
                <div
                  key={tool}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    fontSize: 20,
                    fontWeight: isLatest ? 700 : 500,
                    color: isLatest ? colors.accent : colors.ink,
                    opacity: visible ? 1 : 0.15,
                  }}
                >
                  <span
                    style={{
                      width: 9,
                      height: 9,
                      borderRadius: "50%",
                      background: isLatest ? colors.accent : colors.muted,
                      display: "inline-block",
                      flexShrink: 0,
                    }}
                  />
                  {tool}
                </div>
              );
            })}
          </div>
        </div>

        {/* Panel 3: Web Dashboard & HTTP Daemon */}
        <div style={panelStyle(2)}>
          <div
            style={{
              padding: "14px 24px",
              borderBottom: `1.5px solid ${colors.line}`,
              fontSize: 19,
              fontWeight: 700,
              color: colors.accent,
              letterSpacing: "0.08em",
              background: colors.surface2,
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
            }}
          >
            <span>03 / WEB DASHBOARD &amp; REST API</span>
            <span style={{ color: colors.muted }}>127.0.0.1:7701</span>
          </div>
          <div style={{ padding: "20px 26px", display: "flex", flexDirection: "column", gap: 10 }}>
            {[
              "recall  • backend-agent  • 0.93 sim (exact+hybrid)",
              "learn   • frontend-agent • indexed in 0.08 ms",
              "sync    • deploy-agent   • session summary stored",
            ].map((entry, i) => (
              <div
                key={i}
                style={{
                  fontSize: 20,
                  color: i === 0 ? colors.accent : colors.muted,
                  fontWeight: i === 0 ? 700 : 500,
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                  opacity: interpolate(frame, [70 + i * 20, 90 + i * 20], [0, 1], {
                    extrapolateLeft: "clamp",
                    extrapolateRight: "clamp",
                  }),
                }}
              >
                <span
                  style={{
                    width: 8,
                    height: 8,
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

      {/* Bottom Takeaway Caption */}
      <div
        style={{
          position: "absolute",
          bottom: 215,
          left: MOBILE.padX,
          right: MOBILE.padX,
          textAlign: "center",
          opacity: captionOpacity,
        }}
      >
        <div
          style={{
            fontFamily: fonts.display,
            fontWeight: 800,
            fontSize: 44,
            letterSpacing: "-0.03em",
            color: colors.ink,
          }}
        >
          Every interface included.{" "}
          <span style={{ color: colors.accent }}>Zero lock-in.</span>
        </div>
      </div>
    </AbsoluteFill>
  );
}