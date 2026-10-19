import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { colors, fonts, MOBILE, radius } from "../lib/tokens";
import MeshMark from "../components/MeshMark";
import MeshLogo from "../components/MeshLogo";
import AgentChip from "../components/AgentChip";
import NetworkLine from "../components/NetworkLine";
import GridField from "../components/GridField";
import { useTypewriterLines } from "../hooks/useTypewriter";

const HUB = { x: 540, y: 680 };

const agents = [
  { name: "backend-agent",  x: 270, y: 360 },
  { name: "frontend-agent", x: 810, y: 360 },
  { name: "review-agent",   x: 270, y: 1000 },
  { name: "deploy-agent",   x: 810, y: 1000 },
];

const ctaLines = [
  "$ pip install mesh-context-layer",
  "$ mesh setup --yes",
  "ok  shared agent memory active ",
];

/**
 * Scene 12 — Finale (frames 0–540)
 * Full vertical network glows, tagline lands at 160, fade to dark at 360, CTA end-card types in.
 */
export default function S12_Finale() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const markSpring = spring({
    frame: frame - 5,
    fps,
    config: { damping: 18, stiffness: 80, mass: 1.1 },
  });
  const markSize = interpolate(markSpring, [0, 1], [80, 210]);
  const markOpacity = interpolate(markSpring, [0, 1], [0, 1]);

  const blobOpacity = interpolate(frame, [10, 60], [0, 0.55], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Tagline lands at frame 160 (synced with impact_boom.wav at global 5200)
  const line1Opacity = interpolate(frame, [158, 190], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const line1Y = interpolate(frame, [158, 190], [24, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const line2Opacity = interpolate(frame, [195, 230], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const line2Y = interpolate(frame, [195, 230], [24, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Fade to dark end-card at frame 360
  const fadeStart = 360;
  const fadeOpacity = interpolate(frame, [fadeStart, fadeStart + 45], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // CTA types in after fade (synced with key clicks at global 5450..5540 = scene frames 410..500)
  const ctaStart = fadeStart + 45;
  const ctaTyped = useTypewriterLines(ctaLines, ctaStart, 1.15);
  const ctaOpacity = interpolate(frame, [ctaStart - 10, ctaStart + 15], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const cursorVisible = Math.floor(frame / 24) % 2 === 0;

  const glowPulse = (Math.sin(frame * 0.06) + 1) / 2;
  const glowOpacity =
    interpolate(frame, [30, 80], [0, 0.3], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    }) *
    (0.7 + glowPulse * 0.3);

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
            radial-gradient(circle at 80% 15%, ${colors.accentSoft}, transparent 35%),
            radial-gradient(circle at 20% 75%, ${colors.surface2}, transparent 35%)
          `,
        }}
      />

      <GridField opacity={0.3} mask="center" />

      {/* Network SVG */}
      <svg
        style={{
          position: "absolute",
          inset: 0,
          width: "100%",
          height: "100%",
          pointerEvents: "none",
        }}
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
            strokeWidth={3.5}
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
          left: HUB.x - 190,
          top: HUB.y - 190,
          width: 380,
          height: 380,
          borderRadius: "50%",
          background: `radial-gradient(circle, ${colors.accentSoft} 0%, transparent 70%)`,
          opacity: glowOpacity,
          pointerEvents: "none",
        }}
      />

      {/* Central Hub mark */}
      <div
        style={{
          position: "absolute",
          left: HUB.x - markSize / 2,
          top: HUB.y - markSize / 2,
          opacity: markOpacity,
        }}
      >
        <MeshMark size={markSize} color={colors.accent} animate startFrame={5} />
      </div>

      {/* Wordmark below mark */}
      <div
        style={{
          position: "absolute",
          left: HUB.x,
          top: HUB.y + markSize / 2 + 20,
          transform: "translateX(-50%)",
          fontFamily: fonts.display,
          fontWeight: 800,
          fontSize: 52,
          letterSpacing: "-0.045em",
          color: colors.ink,
          opacity: interpolate(frame, [95, 125], [0, 1], {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
          }),
          whiteSpace: "nowrap",
        }}
      >
        mesh
      </div>

      {/* Agent chips */}
      {agents.map((agent, i) => (
        <div
          key={i}
          style={{
            position: "absolute",
            left: agent.x,
            top: agent.y,
            transform: "translate(-50%, -50%)",
          }}
        >
          <AgentChip
            name={agent.name}
            active={frame > 100 + i * 18}
            entranceFrame={30 + i * 14}
          />
        </div>
      ))}

      {/* Tagline block */}
      <div
        style={{
          position: "absolute",
          bottom: 260,
          left: MOBILE.padX,
          right: MOBILE.padX,
          textAlign: "center",
        }}
      >
        <div
          style={{
            fontFamily: fonts.display,
            fontWeight: 800,
            fontSize: 62,
            letterSpacing: "-0.045em",
            lineHeight: 1.12,
            color: colors.ink,
            opacity: line1Opacity,
            transform: `translateY(${line1Y}px)`,
            marginBottom: 12,
          }}
        >
          Give your agents memory.
        </div>
        <div
          style={{
            fontFamily: fonts.display,
            fontWeight: 800,
            fontSize: 62,
            letterSpacing: "-0.045em",
            lineHeight: 1.12,
            color: colors.accent,
            opacity: line2Opacity,
            transform: `translateY(${line2Y}px)`,
          }}
        >
          Give them each other.
        </div>
      </div>

      {/* Fade to dark overlay for final CTA End-Card */}
      <AbsoluteFill
        style={{
          background: colors.bg,
          opacity: fadeOpacity,
          pointerEvents: "none",
        }}
      />

      {/* Final Mobile End-Card (frames 405–540) */}
      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          padding: `0 ${MOBILE.padX}px`,
          gap: 44,
          opacity: ctaOpacity,
        }}
      >
        <MeshLogo size={140} startFrame={fadeStart} />

        <div
          style={{
            width: "100%",
            background: colors.terminalBg,
            border: `2.5px solid ${colors.accent}`,
            borderRadius: radius.md,
            boxShadow: "0 0 50px rgba(200, 240, 74, 0.2), 8px 8px 0px #000",
            overflow: "hidden",
          }}
        >
          {/* Header */}
          <div
            style={{
              padding: "16px 24px",
              background: colors.surface2,
              borderBottom: `1.5px solid ${colors.terminalLine}`,
              display: "flex",
              alignItems: "center",
              gap: 12,
            }}
          >
            {["#4A4A4A", "#4A4A4A", "#4A4A4A"].map((c, i) => (
              <span
                key={i}
                style={{
                  width: 12,
                  height: 12,
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
                fontSize: 19,
                fontWeight: 600,
                letterSpacing: "0.08em",
                fontFamily: fonts.mono,
              }}
            >
              INSTALL NOW ON PYPI
            </span>
          </div>

          {/* Body */}
          <div
            style={{
              padding: "34px 36px",
              fontFamily: fonts.mono,
              fontSize: 28,
              lineHeight: 1.75,
              background: colors.terminalBg,
              minHeight: 240,
            }}
          >
            {ctaTyped.map((line, i) => {
              const isPrompt = line.startsWith("$");
              return (
                <div key={i} style={{ color: colors.ink, marginBottom: 8 }}>
                  {isPrompt ? (
                    <>
                      <span style={{ color: colors.accent, fontWeight: 700 }}>$ </span>
                      <span style={{ color: colors.ink, fontWeight: 700 }}>
                        {line.replace(/^\$\s*/, "")}
                      </span>
                    </>
                  ) : (
                    <span style={{ color: colors.accent, fontWeight: 600 }}>{line}</span>
                  )}
                  {i === ctaTyped.length - 1 && cursorVisible && (
                    <span style={{ color: colors.accent, fontWeight: 700, marginLeft: 4 }}>
                      █
                    </span>
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
            fontSize: 22,
            fontWeight: 700,
            letterSpacing: "0.1em",
            textTransform: "uppercase",
            color: colors.muted,
            textAlign: "center",
          }}
        >
          MIT Licensed • Python 3.10+ • 100% Local-First
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
}