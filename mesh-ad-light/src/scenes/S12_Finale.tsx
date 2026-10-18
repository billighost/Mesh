import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { colors, fonts, radius } from "../lib/tokens";
import MeshMark from "../components/MeshMark";
import AgentChip from "../components/AgentChip";
import NetworkLine from "../components/NetworkLine";
import GridField from "../components/GridField";
import { useTypewriterLines } from "../hooks/useTypewriter";

const agents = [
  { name: "backend-agent",  x: 960,  y: 260 },
  { name: "frontend-agent", x: 1300, y: 480 },
  { name: "review-agent",   x: 960,  y: 700 },
  { name: "deploy-agent",   x: 620,  y: 480 },
];
const HUB = { x: 960, y: 480 };

const ctaLines = [
  "$ pip install mesh-context-layer",
  "ok  _",
];

/**
 * Scene 12 — Finale (frames 0–540)
 * Full network glows, tagline appears, fade to dark, CTA types.
 */
export default function S12_Finale() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Mark scales up to fill
  const markSpring = spring({
    frame: frame - 5,
    fps,
    config: { damping: 18, stiffness: 80, mass: 1.1 },
  });
  const markSize = interpolate(markSpring, [0, 1], [60, 200]);
  const markOpacity = interpolate(markSpring, [0, 1], [0, 1]);

  // Background blobs pulse in
  const blobOpacity = interpolate(frame, [10, 60], [0, 0.45], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Network lines draw
  // Tagline
  const line1Opacity = interpolate(frame, [160, 200], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const line1Y = interpolate(frame, [160, 200], [20, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const line2Opacity = interpolate(frame, [200, 240], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const line2Y = interpolate(frame, [200, 240], [20, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Fade to dark
  const fadeStart = 360;
  const fadeOpacity = interpolate(frame, [fadeStart, fadeStart + 60], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // CTA types in after fade
  const ctaStart = fadeStart + 50;
  const ctaTyped = useTypewriterLines(ctaLines, ctaStart, 0.8);
  const ctaOpacity = interpolate(frame, [ctaStart, ctaStart + 20], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const cursorVisible = Math.floor(frame / 30) % 2 === 0;

  // Mark pulsing glow at the end
  const glowPulse = (Math.sin(frame * 0.06) + 1) / 2;
  const glowOpacity = interpolate(frame, [30, 80], [0, 0.2], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  }) * (0.7 + glowPulse * 0.3);

  return (
    <AbsoluteFill style={{ background: colors.bg }}>
      {/* Background blobs */}
      <div
        aria-hidden
        style={{
          position: "absolute",
          inset: 0,
          opacity: blobOpacity,
          background: `
            radial-gradient(circle at 82% -6%, ${colors.accentSoft}, transparent 28%),
            radial-gradient(circle at -4% 46%, ${colors.surface2}, transparent 24%)
          `,
        }}
      />

      <GridField opacity={0.3} mask="center" />

      {/* Network SVG */}
      <svg
        style={{ position: "absolute", inset: 0, width: "100%", height: "100%", pointerEvents: "none" }}
      >
        {agents.map((agent, i) => (
          <NetworkLine
            key={i}
            x1={HUB.x}
            y1={HUB.y}
            x2={agent.x}
            y2={agent.y}
            startFrame={40 + i * 14}
            duration={20}
            color={colors.accent}
            strokeWidth={1.5}
            showPacket
            packetDelay={i * 12}
          />
        ))}
      </svg>

      {/* Mark pulsing glow */}
      <div
        aria-hidden
        style={{
          position: "absolute",
          left: HUB.x - 160,
          top: HUB.y - 160,
          width: 320,
          height: 320,
          borderRadius: "50%",
          background: `radial-gradient(circle, ${colors.accentSoft} 0%, transparent 70%)`,
          opacity: glowOpacity,
          pointerEvents: "none",
        }}
      />

      {/* Hub mark */}
      <div
        style={{
          position: "absolute",
          left: HUB.x - markSize / 2,
          top: HUB.y - markSize / 2,
          opacity: markOpacity,
        }}
      >
        <MeshMark
          size={markSize}
          color={colors.ink}
          animate
          startFrame={5}
        />
      </div>

      {/* Agent chips */}
      {agents.map((agent, i) => (
        <div
          key={i}
          style={{
            position: "absolute",
            left: agent.x - 75,
            top: agent.y - 20,
          }}
        >
          <AgentChip
            name={agent.name}
            active={frame > 100 + i * 18}
            entranceFrame={30 + i * 14}
          />
        </div>
      ))}

      {/* Tagline */}
      <div
        style={{
          position: "absolute",
          bottom: 160,
          left: 0,
          right: 0,
          textAlign: "center",
        }}
      >
        <div
          style={{
            fontFamily: fonts.display,
            fontWeight: 600,
            fontSize: 52,
            letterSpacing: "-0.045em",
            lineHeight: 1.05,
            color: colors.ink,
            opacity: line1Opacity,
            transform: `translateY(${line1Y}px)`,
          }}
        >
          Give your agents memory.
        </div>
        <div
          style={{
            fontFamily: fonts.display,
            fontWeight: 600,
            fontSize: 52,
            letterSpacing: "-0.045em",
            lineHeight: 1.05,
            color: colors.accentText,
            opacity: line2Opacity,
            transform: `translateY(${line2Y}px)`,
          }}
        >
          Give them each other.
        </div>
      </div>

      {/* Wordmark below mark */}
      <div
        style={{
          position: "absolute",
          left: HUB.x,
          top: HUB.y + markSize / 2 + 24,
          transform: "translateX(-50%)",
          fontFamily: fonts.display,
          fontWeight: 600,
          fontSize: 32,
          letterSpacing: "-0.045em",
          color: colors.ink,
          opacity: interpolate(frame, [100, 130], [0, 1], {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
          }),
          whiteSpace: "nowrap",
        }}
      >
        mesh
      </div>

      {/* Fade to dark overlay */}
      <AbsoluteFill
        style={{
          background: colors.bg,
          opacity: fadeOpacity,
          pointerEvents: "none",
        }}
      />

      {/* CTA card */}
      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 24,
          opacity: ctaOpacity,
        }}
      >
        <div
          style={{
            background: colors.surface,
            border: `1px solid ${colors.line}`,
            borderRadius: radius.sm,
            boxShadow: `4px 4px 0px ${colors.ink}`,
            overflow: "hidden",
            minWidth: 580,
          }}
        >
          {/* Header */}
          <div
            style={{
              padding: "10px 16px",
              background: colors.surface2,
              borderBottom: `1px solid ${colors.line}`,
              display: "flex",
              alignItems: "center",
              gap: 8,
            }}
          >
            {["#C5CBB9", "#C5CBB9", "#C5CBB9"].map((c, i) => (
              <span
                key={i}
                style={{
                  width: 8,
                  height: 8,
                  borderRadius: "50%",
                  background: c,
                  display: "inline-block",
                }}
              />
            ))}
            <span
              style={{
                marginLeft: 8,
                color: colors.muted,
                fontSize: 11,
                letterSpacing: "0.08em",
                fontFamily: fonts.mono,
              }}
            >
              quickstart
            </span>
          </div>

          {/* Body */}
          <div
            style={{
              padding: "24px 32px",
              fontFamily: fonts.mono,
              fontSize: 22,
              lineHeight: 1.8,
              background: colors.surface,
            }}
          >
            {ctaTyped.map((line, i) => {
              const isPrompt = line.startsWith("$");
              return (
                <div key={i} style={{ color: colors.ink }}>
                  {isPrompt ? (
                    <>
                      <span style={{ color: colors.accentText, fontWeight: 700 }}>$ </span>
                      <span style={{ color: colors.ink, fontWeight: 700 }}>
                        {line.replace(/^\$\s*/, "")}
                      </span>
                    </>
                  ) : (
                    <span style={{ color: colors.muted }}>{line}</span>
                  )}
                  {i === ctaTyped.length - 1 && cursorVisible && (
                    <span style={{ color: colors.accentText, fontWeight: 700 }}>▌</span>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* Sub-label */}
        <div
          style={{
            fontFamily: fonts.mono,
            fontSize: 12,
            letterSpacing: "0.12em",
            textTransform: "uppercase",
            color: colors.muted,
          }}
        >
          MIT Licensed · Python 3.10+ · Local-first
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
}
