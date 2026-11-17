import { useState } from "react";
import { benchmarkData } from "../data/content";
import SectionHeader from "./ui/SectionHeader";
import Badge from "./ui/Badge";
import { TabList } from "./ui/Tabs";

const tabs = [
  { id: "sdk", label: "Core SDK" },
  { id: "mcp", label: "MCP Surface" },
  { id: "scale", label: "At Scale" },
];

function VerdictBadge({ verdict }) {
  const tone = verdict === "fast" ? "ok" : "default";
  const label = verdict === "fast" ? "🟢 Fast" : "🟡 Sub-50ms";
  return <Badge tone={tone}>{label}</Badge>;
}

export default function PerformanceSection() {
  const [tab, setTab] = useState("sdk");

  return (
    <section className="section section--surface" id="performance" aria-labelledby="perf-title">
      <div className="wrap">
        <SectionHeader
          index="05b"
          route="PERFORMANCE"
          id="perf-title"
          title="Sub-millisecond to low-millisecond. On your machine."
          lede="Benchmarked on Mesh v0.8.3, Windows 11 AMD64, Python 3.14.0, SQLite 3.50.4 WAL, local ONNX embeddings. Your hardware will differ — run benchmark_all_surfaces.py to reproduce."
          meta="25 iterations / 2 warmup / perf_counter_ns"
        />

        <div className="perf__tabs-row">
          <TabList
            items={tabs}
            value={tab}
            onChange={setTab}
            idBase="perf"
            label="Benchmark surface"
          />
        </div>

        {tab === "sdk" && (
          <div className="perf__table-wrap" role="region" aria-label="Core SDK benchmarks">
            <table className="perf-table">
              <thead>
                <tr>
                  <th scope="col">Operation</th>
                  <th scope="col" className="perf-table__num">p50</th>
                  <th scope="col" className="perf-table__num">p95</th>
                  <th scope="col">Verdict</th>
                </tr>
              </thead>
              <tbody>
                {benchmarkData.sdk.map((row) => (
                  <tr key={row.op}>
                    <td><code className="inline-code">{row.op}</code></td>
                    <td className="perf-table__num perf-table__num--bold">{row.p50}</td>
                    <td className="perf-table__num">{row.p95}</td>
                    <td><VerdictBadge verdict={row.verdict} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="perf__note">
              ~100 memories, ONNX loaded. <strong>project_brief</strong> and <strong>task_list</strong> stay under 3 ms p50 even as memory grows.
            </p>
          </div>
        )}

        {tab === "mcp" && (
          <div className="perf__table-wrap" role="region" aria-label="MCP surface benchmarks">
            <table className="perf-table">
              <thead>
                <tr>
                  <th scope="col">MCP Tool</th>
                  <th scope="col" className="perf-table__num">p50</th>
                  <th scope="col" className="perf-table__num">p95</th>
                  <th scope="col">Verdict</th>
                </tr>
              </thead>
              <tbody>
                {benchmarkData.mcp.map((row) => (
                  <tr key={row.op}>
                    <td><code className="inline-code">{row.op}</code></td>
                    <td className="perf-table__num perf-table__num--bold">{row.p50}</td>
                    <td className="perf-table__num">{row.p95}</td>
                    <td><VerdictBadge verdict={row.verdict} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="perf__note">
              Measured as called by an agent via the MCP protocol. ~100 memories, ONNX loaded.
            </p>
          </div>
        )}

        {tab === "scale" && (
          <div className="perf__table-wrap" role="region" aria-label="Scale benchmarks">
            <table className="perf-table perf-table--scale">
              <thead>
                <tr>
                  <th scope="col">Memories</th>
                  <th scope="col">Work Items</th>
                  <th scope="col" className="perf-table__num">Recall (p50)</th>
                  <th scope="col" className="perf-table__num">Brief (p50)</th>
                  <th scope="col" className="perf-table__num">Task List (p50)</th>
                  <th scope="col" className="perf-table__num">DB Size</th>
                  <th scope="col" className="perf-table__num">RAM Δ</th>
                </tr>
              </thead>
              <tbody>
                {benchmarkData.scale.map((row) => (
                  <tr key={row.memories}>
                    <td><strong>{row.memories}</strong></td>
                    <td>{row.workItems}</td>
                    <td className="perf-table__num">{row.recall}</td>
                    <td className="perf-table__num perf-table__num--bold">{row.brief}</td>
                    <td className="perf-table__num perf-table__num--bold">{row.taskList}</td>
                    <td className="perf-table__num">{row.dbSize}</td>
                    <td className="perf-table__num">{row.ram}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="perf__note">
              Synthetic corpus, ONNX loaded. <strong>Brief and task list stay under 12 ms</strong> even at 10,000 memories, thanks to composite indices on <code className="inline-code">(namespace, status)</code> and <code className="inline-code">(namespace, completed_at)</code>.
            </p>
          </div>
        )}

        <div className="perf__footer">
          <span className="mono-label">Mesh v0.8.3 · Windows 11 AMD64 / Ubuntu 22.04 CI · Python 3.14.0 · SQLite 3.50.4</span>
          <a
            href="https://github.com/billighost/mesh/blob/main/benchmarks/RESULTS.md"
            target="_blank"
            rel="noopener noreferrer"
            className="perf__results-link"
          >
            Full results →
          </a>
        </div>
      </div>
    </section>
  );
}
