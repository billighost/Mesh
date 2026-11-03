import { useState } from "react";
import { memoryFeatures } from "../data/content";
import Badge from "./ui/Badge";
import Button from "./ui/Button";
import Icon from "./ui/Icon";
import SectionHeader from "./ui/SectionHeader";
import Switch from "./ui/Switch";
import { TabList, TabPanel } from "./ui/Tabs";

const tabs = [
  { id: "dedupe", label: "Dedupe" },
  { id: "contradictions", label: "Contradictions" },
  { id: "ttl", label: "TTL" },
  { id: "privacy", label: "Privacy" },
];

const featureIcons = ["boxes", "shield", "lock", "database"];

const ttlValues = [1, 0.74, 0.55, 0.41];

export default function MemorySection() {
  const [tab, setTab] = useState("dedupe");
  const [merged, setMerged] = useState(false);
  const [resolved, setResolved] = useState(false);
  const [days, setDays] = useState(0);
  const [includePrivate, setIncludePrivate] = useState(false);

  const confidence = ttlValues[days / 30] ?? 0.41;
  const stale = confidence < 0.5;

  return (
    <section className="section" id="memory" aria-labelledby="memory-title">
      <div className="wrap">
        <SectionHeader
          index="04"
          route="MEMORY THAT BEHAVES"
          id="memory-title"
          title="Remember more. Trust it more."
          lede="Shared memory is only useful if it stays honest. Mesh reinforces what repeats, flags what conflicts, ages what goes stale, keeps private facts private, and stores secret pointers instead of values — so the shared memory never becomes a liability."
        />

        <div className="memory__panel">
          <TabList
            items={tabs}
            value={tab}
            onChange={setTab}
            idBase="memory"
            label="Memory behavior"
          />

          {tab === "dedupe" && (
            <TabPanel id="dedupe" idBase="memory">
              <div className="memory__body">
                <div className="memory-demo">
                  <div className="memory-demo__head">
                    <span>dedupe / same fact</span>
                    <Badge tone={merged ? "ok" : "default"}>
                      {merged ? "reinforced" : "2 copies"}
                    </Badge>
                  </div>
                  <div className="mem-stack">
                    <div className="mem-card">
                      <div className="mem-card__top">
                        <span>original / mem-128</span>
                        {merged && <span className="mem-card__badge">×2 reinforced</span>}
                      </div>
                      <p className="mem-card__text">
                        Production PostgreSQL 16 runs on port 5432.
                      </p>
                    </div>
                    <div className={`mem-card ${merged ? "is-merged" : ""}`}>
                      <div className="mem-card__top">
                        <span>duplicate / mem-129</span>
                        <span>new</span>
                      </div>
                      <p className="mem-card__text">
                        Production PG 16 runs on :5432.
                      </p>
                    </div>
                  </div>
                  <div className="mem-actions">
                    <Button variant="secondary" size="sm" onClick={() => setMerged(true)}>
                      Learn it again
                    </Button>
                    <Button variant="quiet" size="sm" onClick={() => setMerged(false)}>
                      Reset
                    </Button>
                  </div>
                  <p className={`mem-status ${merged ? "is-ok" : ""}`} role="status">
                    {merged
                      ? "reinforced in place / ID and history preserved"
                      : "waiting for the duplicate / nothing merged yet"}
                  </p>
                </div>

                <div className="memory-copy">
                  <h3>Dedupe</h3>
                  <p>
                    Identical facts are reinforced, not duplicated. The original ID and
                    history stay intact.
                  </p>
                  <div className="memory-copy__meta">
                    scope / namespace·history preserved·idempotent learn
                  </div>
                </div>
              </div>
            </TabPanel>
          )}

          {tab === "contradictions" && (
            <TabPanel id="contradictions" idBase="memory">
              <div className="memory__body">
                <div className="memory-demo">
                  <div className="memory-demo__head">
                    <span>contradictions / port mapping</span>
                    <Badge tone={resolved ? "ok" : "warn"}>
                      {resolved ? "resolved" : "review"}
                    </Badge>
                  </div>
                  <div className={`conflict-banner ${resolved ? "is-resolved" : ""}`} role="status">
                    <Icon name={resolved ? "check" : "shield"} size={13} />
                    {resolved
                      ? "RESOLVED — 5439 is current"
                      : "CONFLICT DETECTED — review required"}
                  </div>
                  <div className="conflict-grid">
                    <div className={`mem-card ${resolved ? "is-superseded" : ""}`}>
                      <div className="mem-card__top">
                        <span>db-cfg-92</span>
                        <span>old</span>
                      </div>
                      <p className="mem-card__text">Staging database port is 5432.</p>
                      {resolved && <span className="mem-card__badge">superseded</span>}
                    </div>
                    <div className={`mem-card ${resolved ? "is-new" : ""}`}>
                      <div className="mem-card__top">
                        <span>db-cfg-93</span>
                        <span>new</span>
                      </div>
                      <p className="mem-card__text">Staging database port is 5439.</p>
                      {resolved && <span className="mem-card__badge">current</span>}
                    </div>
                  </div>
                  <div className="mem-actions">
                    <Button variant="secondary" size="sm" onClick={() => setResolved(true)}>
                      Resolve
                    </Button>
                    <Button variant="quiet" size="sm" onClick={() => setResolved(false)}>
                      Reset
                    </Button>
                  </div>
                  <p className={`mem-status ${resolved ? "is-ok" : ""}`} role="status">
                    {resolved
                      ? "old value retained / marked superseded / nothing lost"
                      : "one fact conflicts with another / waiting for a decision"}
                  </p>
                </div>

                <div className="memory-copy">
                  <h3>Contradictions</h3>
                  <p>
                    Contradictory facts are flagged for review. Nothing is silently
                    overwritten, and nothing is lost.
                  </p>
                  <div className="memory-copy__meta">
                    scope / namespace·conflict record·explicit resolution
                  </div>
                </div>
              </div>
            </TabPanel>
          )}

          {tab === "ttl" && (
            <TabPanel id="ttl" idBase="memory">
              <div className="memory__body">
                <div className="memory-demo">
                  <div className="memory-demo__head">
                    <span>ttl / confidence decay</span>
                    <Badge tone={stale ? "warn" : "default"}>{stale ? "stale" : "fresh"}</Badge>
                  </div>
                  <div className="ttl-visual">
                    <div className="ttl-readout">
                      <span className="ttl-value">{confidence.toFixed(2)}</span>
                      <span className="ttl-day">day {days}</span>
                    </div>
                    <div className="ttl-track">
                      <div className="ttl-fill" style={{ width: `${confidence * 100}%` }} />
                    </div>
                    <div className="ttl-scale" aria-hidden="true">
                      <span>day 0</span>
                      <span>30</span>
                      <span>60</span>
                      <span>90</span>
                    </div>
                  </div>
                  <div className="mem-actions">
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => setDays((value) => Math.min(90, value + 30))}
                      disabled={days >= 90}
                    >
                      Advance 30 days
                    </Button>
                    <Button variant="quiet" size="sm" onClick={() => setDays(0)}>
                      Reset
                    </Button>
                  </div>
                  <p className={`mem-status ${stale ? "" : "is-ok"}`} role="status">
                    {stale
                      ? "confidence below 0.50 / marked stale for review"
                      : "confidence above 0.50 / still trusted"}
                  </p>
                </div>

                <div className="memory-copy">
                  <h3>TTL</h3>
                  <p>
                    Set a TTL when a fact is temporary. Confidence decays toward stale;{" "}
                    <code className="inline-code">--clear-ttl</code> makes it permanent.
                  </p>
                  <div className="memory-copy__meta">
                    illustrative decay·<code className="inline-code">--ttl &lt;days&gt;</code>
                    ·manual demo
                  </div>
                </div>
              </div>
            </TabPanel>
          )}

          {tab === "privacy" && (
            <TabPanel id="privacy" idBase="memory">
              <div className="memory__body">
                <div className="memory-demo">
                  <div className="memory-demo__head">
                    <span>privacy / local only</span>
                    <Badge tone={includePrivate ? "warn" : "ok"}>
                      {includePrivate ? "included" : "excluded"}
                    </Badge>
                  </div>
                  <div className="mem-card">
                    <div className="mem-card__top">
                      <span>mem-private-04</span>
                      <Badge>local_only</Badge>
                    </div>
                    <p className={`privacy-card__content ${includePrivate ? "" : "is-masked"}`}>
                      {includePrivate
                        ? "Production credentials live in Vault."
                        : "••••••••••••••••••••••••••••"}
                    </p>
                  </div>
                  <ul className="privacy-list">
                    <li>excluded from export</li>
                    <li>redacted in audit, conflicts, and digest</li>
                    <li>excluded from recall by default</li>
                  </ul>
                  <div className="mem-actions">
                    <Switch
                      checked={includePrivate}
                      onChange={setIncludePrivate}
                      label="Include private memories"
                    />
                  </div>
                  <p className={`mem-status ${includePrivate ? "" : "is-ok"}`} role="status">
                    {includePrivate
                      ? "only when explicitly requested (include_private=true)"
                      : "private memories stay excluded by default"}
                  </p>
                </div>

                <div className="memory-copy">
                  <h3>Privacy</h3>
                  <p>
                    Private memories stay private unless a caller explicitly asks for
                    them. Secrets are auto-redacted and forced private.
                  </p>
                  <div className="memory-copy__meta">
                    local_only·include_private·audit scrubbing
                  </div>
                </div>
              </div>
            </TabPanel>
          )}
        </div>

        <div className="feature-grid">
          {memoryFeatures.map((feature, index) => (
            <div className="feature-card" key={feature.title}>
              <div className="feature-card__title">
                <Icon name={featureIcons[index]} size={15} />
                {feature.title}
              </div>
              <p>{feature.body}</p>
            </div>
          ))}
        </div>
        <p className="memory-note">Illustrative states. The package enforces the real behavior.</p>
      </div>
    </section>
  );
}
