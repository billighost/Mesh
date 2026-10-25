import { useCallback, useEffect, useRef, useState } from "react";
import { useInView } from "../hooks/useInView";
import { usePreferences } from "../hooks/usePreferences";
import Badge from "./ui/Badge";
import Button from "./ui/Button";
import Icon from "./ui/Icon";
import Mark from "./ui/Mark";
import SectionHeader from "./ui/SectionHeader";

const stageLabels = ["dense", "bm25", "idf", "syn", "exact"];

const notes = {
  0: "One SQLite file per namespace. No external database daemon.",
  1: "learn / embed 384d / dedupe / hybrid index: dense + BM25 + IDF + synonyms + exact.",
  2: "index / five independent signals, fused into one ranked result.",
  3: "recall / five-stage hybrid search / sanitized context for frontend-agent.",
};

const fact = "Production PostgreSQL 16 runs on port 5432 with pgBouncer connection pooling.";
const query = "what port does postgres use?";

export default function HowItWorks() {
  const { motion } = usePreferences();
  const [step, setStep] = useState(0);
  const [stageRef, inView] = useInView({ threshold: 0.4, once: true });
  const autoPlayed = useRef(false);
  const timers = useRef([]);

  const clearTimers = useCallback(() => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  }, []);

  const run = useCallback(() => {
    clearTimers();
    setStep(0);
    timers.current.push(setTimeout(() => setStep(1), 80));
    timers.current.push(setTimeout(() => setStep(2), 900));
    timers.current.push(setTimeout(() => setStep(3), 1800));
  }, [clearTimers]);

  const learn = () => {
    clearTimers();
    setStep(1);
  };

  const recall = () => {
    clearTimers();
    setStep(3);
  };

  const reset = () => {
    clearTimers();
    setStep(0);
  };

  useEffect(() => {
    if (inView && motion && !autoPlayed.current) {
      autoPlayed.current = true;
      run();
    }
  }, [inView, motion, run]);

  useEffect(() => () => clearTimers(), [clearTimers]);

  return (
    <section className="section" id="how" aria-labelledby="how-title">
      <div className="wrap">
        <SectionHeader
          index="02"
          route="HOW IT WORKS"
          id="how-title"
          title="One agent learns. Every agent knows."
          lede="Agent A learns a production fact — a port, a constraint, a decision. Mesh embeds it locally, deduplicates it, and indexes it five ways. Agent B asks a completely different question hours later in a different session and still gets the right answer. No re-explaining. No wasted tokens."
          meta="simulated example"
        />

        <div className="demo-shell" ref={stageRef}>
          <div className="demo-top">
            <span className="demo-top__label">
              <span className="presence-dot" aria-hidden="true">●</span>
              Live story
              <Badge>example data</Badge>
            </span>
            <div className="step-chips" aria-label="Story progress">
              {["01 Learn", "02 Index", "03 Recall"].map((label, index) => (
                <span
                  key={label}
                  className={`step-chip ${step === index + 1 ? "is-active" : ""}`}
                >
                  {label}
                </span>
              ))}
            </div>
          </div>

          <div className="how__stage">
            <div className={`node ${step >= 1 ? "is-active" : ""}`}>
              <span className="node__label">agent a</span>
              <span className="node__name">backend-agent</span>
              <span className="node__verb">learns</span>
            </div>

            <div className={`how__link ${step >= 1 ? "is-on" : ""}`} aria-hidden="true">
              {step === 1 && <span className="how__packet is-on" />}
            </div>

            <div className={`how__hub ${step === 2 ? "is-indexing" : ""}`}>
              <Mark size={19} />
              <span className="hub__label">shared memory</span>
              <span className="hub__name">mesh</span>
              <div className="hub__stages">
                {stageLabels.map((label) => (
                  <span className={`hub__stage ${step >= 2 ? "is-on" : ""}`} key={label}>
                    {label}
                  </span>
                ))}
              </div>
            </div>

            <div className={`how__link ${step >= 3 ? "is-on" : ""}`} aria-hidden="true">
              {step === 3 && <span className="how__packet is-on" />}
            </div>

            <div className={`node ${step >= 3 ? "is-active" : ""}`}>
              <span className="node__label">agent b</span>
              <span className="node__name">frontend-agent</span>
              <span className="node__verb">recalls</span>
            </div>
          </div>

          <div className="how__readouts">
            <div className="readout">
              <div className="readout__head">
                <span>What backend-agent learned</span>
                {step >= 1 && <Badge tone="ok">learned</Badge>}
              </div>
              {step >= 1 ? (
                <>
                  <p className="readout__fact">{fact}</p>
                  <div className="readout__tags">
                    <Badge>fact</Badge>
                    <Badge>db / prod</Badge>
                    <Badge>384d</Badge>
                  </div>
                </>
              ) : (
                <p className="readout__empty">
                  Nothing learned yet. Run the story to teach backend-agent a fact.
                </p>
              )}
            </div>

            <div className="readout">
              <div className="readout__head">
                <span>What frontend-agent recalled</span>
                {step >= 3 && <Badge tone="accent">recalled</Badge>}
              </div>
              {step >= 3 ? (
                <>
                  <p className="readout__query">query: {query}</p>
                  <p className="readout__fact">{fact}</p>
                  <div className="readout__tags">
                    <Badge>0.93 similarity</Badge>
                    <Badge>from backend-agent</Badge>
                    <Badge>&lt;0.1 ms</Badge>
                  </div>
                </>
              ) : (
                <p className="readout__empty">
                  frontend-agent has not asked yet. It will recall the fact without ever
                  talking to backend-agent.
                </p>
              )}
            </div>
          </div>

          <div className="how__controls">
            <Button icon="play" onClick={run}>Run the story</Button>
            <Button variant="secondary" onClick={learn}>Agent A learns</Button>
            <Button variant="secondary" onClick={recall}>Agent B recalls</Button>
            <Button variant="quiet" icon="replay" onClick={reset}>Reset</Button>
          </div>

          <p className="how__note" role="status" aria-live="polite">
            {notes[step]}
          </p>
        </div>
      </div>
    </section>
  );
}
