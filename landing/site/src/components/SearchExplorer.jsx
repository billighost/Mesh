import { useCallback, useEffect, useRef, useState } from "react";
import { searchSamples, stageMeta } from "../data/content";
import { usePreferences } from "../hooks/usePreferences";
import Badge from "./ui/Badge";
import Button from "./ui/Button";
import SectionHeader from "./ui/SectionHeader";
import Segmented from "./ui/Segmented";
import Tilt from "./ui/Tilt";

export default function SearchExplorer() {
  const { motion } = usePreferences();
  const [query, setQuery] = useState(searchSamples[0].query);
  const [sampleId, setSampleId] = useState(searchSamples[0].id);
  const [mode, setMode] = useState("hybrid");
  const [revealed, setRevealed] = useState(5);
  const [running, setRunning] = useState(false);
  const [message, setMessage] = useState("");
  const [status, setStatus] = useState("Example result ready.");
  const timer = useRef(null);

  const sample = searchSamples.find((item) => item.id === sampleId) || searchSamples[0];
  const fused = mode === "hybrid" ? sample.fused : sample.vectorOnly;
  const stageCount = mode === "hybrid" ? 5 : 1;

  const clearTimer = useCallback(() => {
    if (timer.current) {
      clearInterval(timer.current);
      timer.current = null;
    }
  }, []);

  const run = useCallback(
    (text = query, id = sampleId) => {
      if (!text.trim()) {
        setMessage("Type a query to run the example.");
        setStatus("No query entered.");
        return;
      }
      clearTimer();
      setMessage("");
      setSampleId(id);
      setStatus("Searching five stages…");
      if (!motion) {
        setRevealed(5);
        setRunning(false);
        setStatus("Recall complete. Example result ready.");
        return;
      }
      setRunning(true);
      setRevealed(0);
      let count = 0;
      timer.current = setInterval(() => {
        count += 1;
        setRevealed(count);
        if (count >= 5) {
          clearInterval(timer.current);
          timer.current = null;
          setRunning(false);
          setStatus("Recall complete. Example result ready.");
        }
      }, 120);
    },
    [clearTimer, motion, query, sampleId]
  );

  useEffect(() => () => clearTimer(), [clearTimer]);

  const pickSample = (item) => {
    setQuery(item.query);
    run(item.query, item.id);
  };

  const changeMode = (next) => {
    setMode(next);
    setRevealed(5);
    setRunning(false);
    clearTimer();
    setStatus(
      next === "hybrid"
        ? "Hybrid result ready. Five stages fused."
        : "Vector-only result ready. Four signal stages skipped."
    );
  };

  return (
    <section className="section" id="search" aria-labelledby="search-title">
      <div className="wrap">
        <SectionHeader
          index="03"
          route="THE RETRIEVAL ENGINE"
          id="search-title"
          title="One query. Five independent signals."
          lede="Most memory tools pick one trick. Mesh runs five retrieval stages and fuses the evidence. That's how it finds a fact when the wording is different, the keyword is rare, or the identifier is exact."
          meta="example data / no network"
        />

        <div className="search__grid">
          <div className="search-controls">
            <div className="field">
              <label className="field__label" htmlFor="mesh-query">Query</label>
              <div className="query-row">
                <input
                  className="query-input"
                  id="mesh-query"
                  value={query}
                  onChange={(event) => {
                    setQuery(event.target.value);
                    if (message) setMessage("");
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      run();
                    }
                  }}
                  placeholder="Ask the mesh anything"
                  autoComplete="off"
                  spellCheck="false"
                />
                <Button icon="search" onClick={() => run()} disabled={running}>
                  {running ? "Running…" : "Run hybrid search"}
                </Button>
              </div>
              {message && <p className="search__footnote" role="status">{message}</p>}
            </div>

            <div className="sample-chips" aria-label="Example queries">
              {searchSamples.map((item) => (
                <button
                  type="button"
                  className={`sample-chip ${sample.id === item.id ? "is-active" : ""}`}
                  key={item.id}
                  onClick={() => pickSample(item)}
                >
                  {item.query}
                </button>
              ))}
            </div>

            <div className="mode-row">
              <Segmented
                options={[
                  { value: "hybrid", label: "Hybrid" },
                  { value: "vector", label: "Vector only" },
                ]}
                value={mode}
                onChange={changeMode}
                label="Search mode"
              />
              <span className="mode-row__hint">
                {mode === "hybrid"
                  ? "five signals / one ranked result"
                  : "dense only / exact terms skipped"}
              </span>
            </div>

            <div className="stage-list">
              {stageMeta.map((stage, index) => {
                const skipped = mode === "vector" && index > 0;
                const active = skipped
                  ? false
                  : mode === "vector"
                    ? revealed > 0
                    : index < revealed;
                const score = sample.stages[index];
                return (
                  <div
                    className={`stage-row ${active ? "is-done" : ""} ${skipped ? "is-skipped" : ""}`}
                    key={stage.key}
                  >
                    <div className="stage-row__top">
                      <span className="stage-row__name">
                        <span className="stage-row__num">{String(index + 1).padStart(2, "0")}</span>
                        {stage.name}
                        <span className="stage-row__tag">{stage.tag}</span>
                      </span>
                      <span className="stage-row__score">
                        {skipped ? "skipped" : score.score.toFixed(2)}
                      </span>
                    </div>
                    <p className="stage-row__desc">{stage.desc}</p>
                    <div className="stage-row__track">
                      <div
                        className="stage-row__fill"
                        style={{ width: active ? `${score.score * 100}%` : "0%" }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
            <p className="search__footnote">
              Illustrative, deterministic scores. Real recall depends on your corpus and
              embedding model. No network request is made.
            </p>
            <p className="visually-hidden" role="status" aria-live="polite">{status}</p>
          </div>

          <div className="search__result">
            <Tilt className="result-card">
              <div className="result-card__head">
                <span className="mono-label">recalled fact</span>
                <Badge tone={mode === "hybrid" ? "ok" : "warn"}>
                  {mode === "hybrid" ? "hybrid" : "vector only"}
                </Badge>
              </div>
              <div className="fused">
                <span className="fused__value">{fused.toFixed(2)}</span>
                <span className="fused__meta">
                  fused confidence
                  <br />
                  {stageCount} / 5 stages
                </span>
              </div>
              <p className="result__fact">{sample.fact}</p>
              <div className="result__meta">
                <Badge>{sample.source}</Badge>
                {sample.tags.map((tag) => <Badge key={tag}>{tag}</Badge>)}
                <Badge>{sample.latency}</Badge>
              </div>
              <p className="result__note">
                {mode === "hybrid"
                  ? "Five signals. One ranked result."
                  : "Dense only. Exact identifiers and rare terms lose their boost."}
                <br />
                example data / local only
              </p>
            </Tilt>
          </div>
        </div>
      </div>
    </section>
  );
}
