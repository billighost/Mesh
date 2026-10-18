import React from "react";
import { Audio, Sequence, staticFile } from "remotion";

/**
 * Complete Soundscape for Mesh Motion Graphics Ad (Light Variant)
 * Perfectly synced to the 93s timeline (5,580 frames at 60fps)
 */
export default function Soundscape() {
  return (
    <>
      {/* 1. Master ElevenLabs Voiceover Track */}
      <Audio src={staticFile("voiceover.mp3")} volume={1.0} />

      {/* 2. Low-frequency Cinematic Ambient Drone bed */}
      <Audio src={staticFile("sfx/ambient_drone.wav")} volume={0.18} />

      {/* ─── Scene 0: Cold Open (0s – 3s / frames 0–180) ────────────────── */}
      {/* Three sequential electronic pings for the 3 mark nodes */}
      <Sequence from={10} durationInFrames={30}>
        <Audio src={staticFile("sfx/mark_node_ping.wav")} volume={0.35} />
      </Sequence>
      <Sequence from={24} durationInFrames={30}>
        <Audio src={staticFile("sfx/mark_node_ping.wav")} volume={0.35} />
      </Sequence>
      <Sequence from={38} durationInFrames={30}>
        <Audio src={staticFile("sfx/mark_node_ping.wav")} volume={0.4} />
      </Sequence>

      {/* ─── Scene 1: The Problem (3s – 12s / frames 180–720) ───────────── */}
      {/* Windows slide in whoosh */}
      <Sequence from={190} durationInFrames={40}>
        <Audio src={staticFile("sfx/whoosh_fast.wav")} volume={0.25} />
      </Sequence>
      {/* Typing burst on agent windows */}
      <Sequence from={210} durationInFrames={120}>
        <Audio src={staticFile("sfx/key_click.wav")} volume={0.3} />
      </Sequence>
      {/* Connection link break & glitch buzz (frame 270 = scene frame 90) */}
      <Sequence from={270} durationInFrames={35}>
        <Audio src={staticFile("sfx/connection_glitch.wav")} volume={0.5} />
      </Sequence>

      {/* ─── Scene 2: Hero Reveal (12s – 18s / frames 720–1080) ─────────── */}
      {/* Massive sub-bass impact boom on lime burst (frame 720) */}
      <Sequence from={722} durationInFrames={110}>
        <Audio src={staticFile("sfx/impact_boom.wav")} volume={0.65} />
      </Sequence>
      {/* Airy whoosh for mark spring */}
      <Sequence from={724} durationInFrames={40}>
        <Audio src={staticFile("sfx/whoosh_fast.wav")} volume={0.35} />
      </Sequence>
      {/* Crystal ping on wordmark lockup */}
      <Sequence from={760} durationInFrames={45}>
        <Audio src={staticFile("sfx/crystallize.wav")} volume={0.35} />
      </Sequence>

      {/* ─── Scene 3: Learn (18s – 28s / frames 1080–1680) ──────────────── */}
      {/* Terminal slide in */}
      <Sequence from={1085} durationInFrames={35}>
        <Audio src={staticFile("sfx/whoosh_fast.wav")} volume={0.2} />
      </Sequence>
      {/* Key clicks during python learn command typing */}
      {[1120, 1145, 1170, 1195, 1220, 1260, 1290, 1320, 1350].map((f) => (
        <Sequence key={f} from={f} durationInFrames={10}>
          <Audio src={staticFile("sfx/key_click.wav")} volume={0.22} />
        </Sequence>
      ))}
      <Sequence from={1380} durationInFrames={15}>
        <Audio src={staticFile("sfx/key_return.wav")} volume={0.35} />
      </Sequence>
      {/* Memory card crystallize pop (frame 1420 = scene frame 340) */}
      <Sequence from={1420} durationInFrames={50}>
        <Audio src={staticFile("sfx/crystallize.wav")} volume={0.55} />
      </Sequence>

      {/* ─── Scene 4: Recall (28s – 38s / frames 1680–2280) ─────────────── */}
      {/* Second terminal typing */}
      {[1720, 1750, 1780, 1820, 1850].map((f) => (
        <Sequence key={f} from={f} durationInFrames={10}>
          <Audio src={staticFile("sfx/key_click.wav")} volume={0.22} />
        </Sequence>
      ))}
      <Sequence from={1880} durationInFrames={15}>
        <Audio src={staticFile("sfx/key_return.wav")} volume={0.35} />
      </Sequence>
      {/* Radar sonar pulse wave radiates (frame 1900 = scene frame 220) */}
      <Sequence from={1900} durationInFrames={75}>
        <Audio src={staticFile("sfx/sonar_pulse.wav")} volume={0.5} />
      </Sequence>
      {/* Telemetry packet travels to memory card (frame 1960) */}
      <Sequence from={1960} durationInFrames={20}>
        <Audio src={staticFile("sfx/packet_transmit.wav")} volume={0.4} />
      </Sequence>
      {/* Memory card hit & glow chime (frame 1980) */}
      <Sequence from={1980} durationInFrames={50}>
        <Audio src={staticFile("sfx/crystallize.wav")} volume={0.45} />
      </Sequence>

      {/* ─── Scene 5: 5-Stage Hybrid Search (38s – 48s / frames 2280–2880) ── */}
      {/* 5 ascending pentatonic chimes as each stage pops its checkmark */}
      <Sequence from={2340} durationInFrames={40}>
        <Audio src={staticFile("sfx/stage_chime_1.wav")} volume={0.4} />
      </Sequence>
      <Sequence from={2420} durationInFrames={40}>
        <Audio src={staticFile("sfx/stage_chime_2.wav")} volume={0.42} />
      </Sequence>
      <Sequence from={2500} durationInFrames={40}>
        <Audio src={staticFile("sfx/stage_chime_3.wav")} volume={0.45} />
      </Sequence>
      <Sequence from={2580} durationInFrames={40}>
        <Audio src={staticFile("sfx/stage_chime_4.wav")} volume={0.48} />
      </Sequence>
      <Sequence from={2660} durationInFrames={45}>
        <Audio src={staticFile("sfx/stage_chime_5.wav")} volume={0.55} />
      </Sequence>

      {/* ─── Scene 6: Multi-Agent Network (48s – 58s / frames 2880–3480) ── */}
      {/* Hub mark pulse */}
      <Sequence from={2890} durationInFrames={30}>
        <Audio src={staticFile("sfx/crystallize.wav")} volume={0.35} />
      </Sequence>
      {/* Network lines draw & telemetry packets fly to agents */}
      <Sequence from={2930} durationInFrames={20}>
        <Audio src={staticFile("sfx/packet_transmit.wav")} volume={0.35} />
      </Sequence>
      <Sequence from={2950} durationInFrames={20}>
        <Audio src={staticFile("sfx/packet_transmit.wav")} volume={0.35} />
      </Sequence>
      <Sequence from={2970} durationInFrames={20}>
        <Audio src={staticFile("sfx/packet_transmit.wav")} volume={0.35} />
      </Sequence>
      <Sequence from={2990} durationInFrames={20}>
        <Audio src={staticFile("sfx/packet_transmit.wav")} volume={0.35} />
      </Sequence>

      {/* ─── Scene 7: Contradictions (58s – 64s / frames 3480–3840) ─────── */}
      {/* Conflict amber warning buzz (frame 3580 = scene frame 100) */}
      <Sequence from={3580} durationInFrames={35}>
        <Audio src={staticFile("sfx/warn_buzz.wav")} volume={0.45} />
      </Sequence>
      {/* Resolution major triad bell (frame 3700 = scene frame 220) */}
      <Sequence from={3700} durationInFrames={55}>
        <Audio src={staticFile("sfx/resolved_bell.wav")} volume={0.5} />
      </Sequence>

      {/* ─── Scene 8: TTL Decay (64s – 70s / frames 3840–4200) ──────────── */}
      {/* Downward decay filter sweep */}
      <Sequence from={3940} durationInFrames={50}>
        <Audio src={staticFile("sfx/decay_filter.wav")} volume={0.35} />
      </Sequence>

      {/* ─── Scene 9: Privacy (70s – 74s / frames 4200–4440) ────────────── */}
      {/* Shield deflect ping (frame 4358 = scene frame 158) */}
      <Sequence from={4358} durationInFrames={30}>
        <Audio src={staticFile("sfx/shield_deflect.wav")} volume={0.45} />
      </Sequence>

      {/* ─── Scene 10: Interfaces (74s – 80s / frames 4440–4800) ────────── */}
      {/* Panel 1, 2, 3 whooshes */}
      <Sequence from={4445} durationInFrames={25}>
        <Audio src={staticFile("sfx/whoosh_fast.wav")} volume={0.25} />
      </Sequence>
      <Sequence from={4465} durationInFrames={25}>
        <Audio src={staticFile("sfx/whoosh_fast.wav")} volume={0.25} />
      </Sequence>
      <Sequence from={4485} durationInFrames={25}>
        <Audio src={staticFile("sfx/whoosh_fast.wav")} volume={0.25} />
      </Sequence>

      {/* ─── Scene 11: Install (80s – 84s / frames 4800–5040) ───────────── */}
      {[4840, 4865, 4890, 4920].map((f) => (
        <Sequence key={f} from={f} durationInFrames={10}>
          <Audio src={staticFile("sfx/key_click.wav")} volume={0.25} />
        </Sequence>
      ))}
      <Sequence from={4945} durationInFrames={15}>
        <Audio src={staticFile("sfx/key_return.wav")} volume={0.35} />
      </Sequence>

      {/* ─── Scene 12: Finale (84s – 93s / frames 5040–5580) ────────────── */}
      {/* Riser swell leading into final tagline */}
      <Sequence from={5050} durationInFrames={180}>
        <Audio src={staticFile("sfx/riser_swell.wav")} volume={0.4} />
      </Sequence>
      {/* Cinematic sub-impact boom when tagline lands (frame 5200) */}
      <Sequence from={5200} durationInFrames={100}>
        <Audio src={staticFile("sfx/impact_boom.wav")} volume={0.65} />
      </Sequence>
      {/* Final pip install terminal typing */}
      {[5450, 5475, 5500, 5520].map((f) => (
        <Sequence key={f} from={f} durationInFrames={10}>
          <Audio src={staticFile("sfx/key_click.wav")} volume={0.28} />
        </Sequence>
      ))}
      <Sequence from={5540} durationInFrames={20}>
        <Audio src={staticFile("sfx/key_return.wav")} volume={0.4} />
      </Sequence>
    </>
  );
}
