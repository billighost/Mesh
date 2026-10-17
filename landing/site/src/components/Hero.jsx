import { useState } from "react";
import { GITHUB_URL, INSTALL_CMD } from "../data/content";
import { useCopy } from "../hooks/useCopy";
import Button from "./ui/Button";
import Mark from "./ui/Mark";
import Terminal from "./ui/Terminal";

const heroLines = [
  "$ mesh status",
  "ok  local / sqlite wal / 384d",
  '$ backend.learn("postgres runs on :5432")',
  "ok  indexed / dedupe on / 0.08 ms",
  '$ frontend.recall("postgres port")',
  "ok  1 hit / 0.93 sim / backend-agent",
  "ok  shared with 3 agents",
  "ok  _",
];

export default function Hero() {
  const [, copy] = useCopy();
  const [replayKey, setReplayKey] = useState(0);

  return (
    <section className="hero section section--flush" id="top" aria-labelledby="hero-title">
      <div className="grid-field" aria-hidden="true" />
      <div className="hero__glow" aria-hidden="true" />
      <div className="wrap hero__grid">
        <div className="hero__copy">
          <span className="kicker">Shared memory for AI agents</span>
          <h1 className="hero__title" id="hero-title">
            <span className="hero__line">Your agents are</span>
            <span className="hero__line">brilliant.</span>
            <span className="hero__line">And they have</span>
            <span className="hero__line">
              <span className="hero__highlight">amnesia.</span>
            </span>
          </h1>
          <p className="hero__lede">
            Mesh gives every agent one shared memory. One learns a fact. Every other agent
            recalls it in under a tenth of a millisecond.
          </p>
          <div className="hero__actions">
            <Button
              icon="copy"
              onClick={() => copy(INSTALL_CMD, "Install command copied")}
            >
              Install mesh
            </Button>
            <Button href="#how" variant="secondary" iconRight="arrowDown">
              See it work
            </Button>
            <Button href={GITHUB_URL} variant="quiet" iconRight="arrowUpRight">
              Read the docs
            </Button>
          </div>
          <div className="hero__meta">
            <span>MIT</span>
            <span className="hero__meta-sep">/</span>
            <span>local-first</span>
            <span className="hero__meta-sep">/</span>
            <span>Python 3.10+</span>
            <span className="hero__meta-sep">/</span>
            <span>no external database</span>
          </div>
        </div>

        <div className="hero__visual">
          <div className="hero__terminal-wrap">
            <Terminal
              className="hero__terminal"
              title="mesh / checkout-service"
              lines={heroLines}
              replayKey={replayKey}
              onReplay={() => setReplayKey((value) => value + 1)}
              caption="example session / local only"
            />
          </div>
          <div className="hero__flow" aria-label="Example flow: backend-agent learns, mesh shares, frontend-agent recalls">
            <span className="flow-chip">
              <span className="presence-dot" aria-hidden="true" />
              backend-agent
            </span>
            <span className="flow-arrow" aria-hidden="true">→</span>
            <span className="flow-chip is-hub">
              <Mark size={11} />
              mesh
            </span>
            <span className="flow-arrow" aria-hidden="true">→</span>
            <span className="flow-chip">
              <span className="presence-dot" aria-hidden="true" />
              frontend-agent
            </span>
          </div>
          <p className="hero__caption">learn → recall / one shared namespace</p>
        </div>
      </div>
    </section>
  );
}
