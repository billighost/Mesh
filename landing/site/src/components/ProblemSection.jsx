import { useState } from "react";
import Badge from "./ui/Badge";
import Icon from "./ui/Icon";
import Mark from "./ui/Mark";
import SectionHeader from "./ui/SectionHeader";
import Segmented from "./ui/Segmented";

const agents = [
  { label: "claude / terminal", copy: "copy 1" },
  { label: "cursor / editor", copy: "copy 2" },
  { label: "codex / review", copy: "copy 3" },
];

export default function ProblemSection() {
  const [mode, setMode] = useState("without");

  return (
    <section className="section" id="problem" aria-labelledby="problem-title">
      <div className="wrap">
        <SectionHeader
          index="01"
          route="THE PROBLEM"
          id="problem-title"
          title="Every session starts from zero."
          lede="Your terminal agent discovers the staging port. Your editor agent never hears about it. Your review agent re-learns the same constraint for the third time this week. The work is already done — it just isn't anywhere your next agent can reach."
        />

        <div className="problem__toolbar">
          <Segmented
            options={[
              { value: "without", label: "Without mesh" },
              { value: "with", label: "With mesh" },
            ]}
            value={mode}
            onChange={setMode}
            label="Compare agent memory with and without Mesh"
          />
          <span className="problem__caption">illustrative diagram</span>
        </div>

        <div className="problem-panel">
          <div className="problem-panel__inner" key={mode}>
            <div className="problem-panel__head">
              <span className="mono-label">
                {mode === "without" ? "without mesh / three silos" : "with mesh / one shared memory"}
              </span>
              <Badge tone={mode === "without" ? "warn" : "ok"}>
                {mode === "without" ? "3 copies" : "1 shared fact"}
              </Badge>
            </div>

            {mode === "without" ? (
              <>
                <div className="without-grid">
                  {agents.map((agent) => (
                    <div className="agent-mini" key={agent.label}>
                      <div className="agent-mini__bar">
                        <span className="agent-mini__name">
                          <span className="agent-mini__dot" aria-hidden="true" />
                          {agent.label}
                        </span>
                        <span className="agent-mini__x" aria-label="not shared">×</span>
                      </div>
                      <p className="agent-mini__fact">
                        Production PostgreSQL 16 runs on port 5432.
                      </p>
                      <span className="agent-mini__copy">{agent.copy} / learned again</span>
                    </div>
                  ))}
                </div>
                <div className="tally">
                  <span>same fact / three silos</span>
                  <span><strong>3 copies</strong> / 0 shared</span>
                </div>
              </>
            ) : (
              <>
                <div className="with-map">
                  <div className="map-hub">
                    <div className="map-hub__top">
                      <Mark size={13} />
                      shared namespace / checkout-service
                    </div>
                    <p className="map-hub__fact">
                      Production PostgreSQL 16 runs on port 5432.
                    </p>
                  </div>
                  <div className="map-connector" aria-hidden="true" />
                  <div className="map-agents">
                    {agents.map((agent) => (
                      <div className="map-agent" key={agent.label}>
                        <span className="map-agent__name">
                          <Icon name="check" size={11} />
                          {agent.label}
                        </span>
                        <span className="map-agent__state">recalls</span>
                      </div>
                    ))}
                  </div>
                </div>
                <div className="tally">
                  <span>same fact / one hub</span>
                  <span><strong>1 copy</strong> / 3 agents</span>
                </div>
              </>
            )}
          </div>
        </div>

        <blockquote className="pullquote">
          The amnesia tax is paid in tokens, latency, and repeated mistakes.
        </blockquote>
      </div>
    </section>
  );
}
