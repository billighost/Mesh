import { faq, GITHUB_URL } from "../data/content";
import Accordion from "./ui/Accordion";
import Icon from "./ui/Icon";
import SectionHeader from "./ui/SectionHeader";

export default function FaqSection() {
  return (
    <section className="section section--surface" id="faq" aria-labelledby="faq-title">
      <div className="wrap">
        <SectionHeader
          index="09"
          route="FAQ"
          id="faq-title"
          title="Questions, answered."
          lede="No mystery cloud, no hidden daemon, no silent downloads. Here are the details that matter before you install."
        />
        <div className="faq__wrap">
          <Accordion items={faq} idBase="faq" defaultOpen={0} />
          <aside className="faq__aside">
            <span className="mono-label">still curious?</span>
            <p>
              The README covers the threat model, consent prompts, namespaces, MCP tools,
              and every CLI flag. Read it before deploying in a shared environment.
            </p>
            <a href={GITHUB_URL} target="_blank" rel="noopener noreferrer">
              Read the README
              <Icon name="arrowUpRight" size={13} />
              <span className="visually-hidden">(opens in a new tab)</span>
            </a>
          </aside>
        </div>
      </div>
    </section>
  );
}
