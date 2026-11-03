import { Fragment } from "react";
import { architecture, stats } from "../data/content";
import SectionHeader from "./ui/SectionHeader";

const specRows = [
  ["Storage", "SQLite WAL per namespace, ~/.mesh/, mode 0700"],
  ["Embeddings", "all-MiniLM-L6-v2 ONNX, 384d, optional ~90 MB download"],
  ["Fallback", "hash-384 feature-hash engine, <0.05 ms, auto-heals when ONNX arrives"],
  ["Retrieval", "dense + BM25 + IDF + synonyms + exact tokens — 5 stages fused"],
  ["Interfaces", "MCP (17 tools), HTTP REST, CLI, Python SDK, dashboard"],
  ["Tasks / Goals", "work_items table, separate from memories — status, priority, events, evidence"],
  ["Secrets", "secret_ref pointers (env://, keyring://, op://), redaction, OS keyring vault"],
  ["Runtime", "Python 3.10+, MIT, v0.8.3"],
  ["Network", "Local by default; cloud log sync and update checks are opt-in"],
];

export default function UnderTheHood() {
  return (
    <section className="section section--surface" id="under-the-hood" aria-labelledby="under-title">
      <div className="wrap">
        <SectionHeader
          index="05"
          route="UNDER THE HOOD"
          id="under-title"
          title="A small local engine. No cloud dependency."
          lede="Mesh stores memory in SQLite WAL, embeds locally via ONNX, fuses five retrieval signals, tracks goals and tasks in a separate ledger, and handles secret pointers — all in one package, all on your machine."
          meta="local engine / documented figures"
        />

        <div className="stats">
          {stats.map((stat) => (
            <div className="stat" key={stat.label}>
              <div className="stat__value">{stat.value}</div>
              <div className="stat__label">{stat.label}</div>
            </div>
          ))}
        </div>

        <div className="arch" aria-label="Architecture: agents connect through interfaces to the core and local storage">
          {architecture.map((group, index) => (
            <Fragment key={group.title}>
              <div className={`arch__group ${group.hot ? "arch__group--hot" : ""}`}>
                <div className="arch__title">{group.title}</div>
                <div className="arch__items">
                  {group.items.map((item) => (
                    <span className="arch__item" key={item}>{item}</span>
                  ))}
                </div>
              </div>
              {index < architecture.length - 1 && (
                <span className="arch__arrow" aria-hidden="true">→</span>
              )}
            </Fragment>
          ))}
        </div>
        <div className="arch__foot">
          <span className="presence-dot" aria-hidden="true">●</span>
          local only · no external daemon · mode 0700 · 17 MCP tools
        </div>

        <div className="under__grid">
          <dl className="spec-table">
            {specRows.map(([label, value]) => (
              <div className="spec-row" key={label}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>

          <div className="doctor">
            <div className="doctor__bar">example output / mesh doctor</div>
            <div className="doctor__body">
              <div><span className="term-ok">ok</span>sqlite fts5 / porter stemming</div>
              <div><span className="term-ok">ok</span>permissions 0700</div>
              <div><span className="term-ok">ok</span>onnx sha256 / 384d model</div>
              <div><span className="term-ok">ok</span>17 mcp tools defined</div>
              <div><span className="term-ok">ok</span>daemon + mcp config</div>
            </div>
          </div>
        </div>

        <p className="footnote">
          Performance figures are from{" "}
          <code className="inline-code">benchmarks/RESULTS.md</code>{" "}
          — Mesh v0.8.3, Windows 11 AMD64, Python 3.14.0. Run{" "}
          <code className="inline-code">benchmark_all_surfaces.py</code>{" "}
          to measure on your hardware.
        </p>
      </div>
    </section>
  );
}
