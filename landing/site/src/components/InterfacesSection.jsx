import { useState } from "react";
import { AGENSI_URL, agents, interfaces } from "../data/content";
import Badge from "./ui/Badge";
import CodeBlock from "./ui/CodeBlock";
import Icon from "./ui/Icon";
import SectionHeader from "./ui/SectionHeader";
import { TabList } from "./ui/Tabs";

const tabs = interfaces.map(({ id, label }) => ({ id, label }));

export default function InterfacesSection() {
  const [tab, setTab] = useState("python");
  const active = interfaces.find((item) => item.id === tab) || interfaces[0];

  return (
    <section className="section" id="interfaces" aria-labelledby="interfaces-title">
      <div className="wrap">
        <SectionHeader
          index="06"
          route="INTERFACES"
          id="interfaces-title"
          title="Plug it into the agents you already run."
          lede="Mesh speaks MCP, HTTP, the CLI, and Python. Start with one interface; the same namespace is available to every other."
          meta="27 agents via Agensi"
        />

        <div className="compat">
          <div className="compat__head">
            <span>works with the agents you already use</span>
            <Badge tone="accent">27 agents</Badge>
          </div>
          <div className="compat__grid">
            {agents.map((agent) => (
              <span className="chip" key={agent}>{agent}</span>
            ))}
            <a
              className="chip chip--button"
              href={AGENSI_URL}
              target="_blank"
              rel="noopener noreferrer"
            >
              +18 via Agensi
              <Icon name="arrowUpRight" size={11} />
              <span className="visually-hidden">(opens in a new tab)</span>
            </a>
          </div>
          <p className="compat__note">
            Via Agensi, Mesh is listed for 27 agents. The same package works with any MCP
            client, or directly through Python, the CLI, and the local HTTP API.
          </p>
        </div>

        <div className="interfaces__panel">
          <TabList
            items={tabs}
            value={tab}
            onChange={setTab}
            idBase="interfaces"
            label="Interface examples"
          />
          <div className="interfaces__body">
            <CodeBlock code={active.code} lang={active.lang} />
            <div className="interface-side">
              <span className="interface-side__label">from the README</span>
              <p className="interface-side__note">{active.note}</p>
              <div className="interface-side__meta">
                <strong>namespace</strong> / one shared memory
                <br />
                <strong>auth</strong> / local token
                <br />
                <strong>network</strong> / loopback by default
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
