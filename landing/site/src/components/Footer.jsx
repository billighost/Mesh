import {
  AGENSI_URL,
  GITHUB_URL,
  PYPI_URL,
} from "../data/content";
import Icon from "./ui/Icon";
import Mark from "./ui/Mark";
import FooterWordmark from "./ui/FooterWordmark";

const columns = [
  {
    title: "Product",
    links: [
      ["How it works", "#how"],
      ["Search", "#search"],
      ["Memory", "#memory"],
      ["Performance", "#performance"],
      ["Install", "#install"],
    ],
  },
  {
    title: "Interfaces",
    links: [
      ["MCP (17 tools)", "#interfaces"],
      ["HTTP REST", "#interfaces"],
      ["CLI", "#interfaces"],
      ["Python SDK", "#interfaces"],
    ],
  },
  {
    title: "Resources",
    links: [
      ["GitHub", GITHUB_URL],
      ["PyPI", PYPI_URL],
      ["Changelog", "https://github.com/billighost/mesh/blob/main/CHANGELOG.md"],
      ["Docs", "https://github.com/billighost/mesh/blob/main/DOCS.md"],
      ["Agensi", AGENSI_URL],
    ],
  },
  {
    title: "Legal",
    links: [["MIT License", "https://opensource.org/license/mit"]],
  },
];


function FooterLink({ href, children }) {
  const external = /^https?:/.test(href);
  return (
    <a
      className="footer__link"
      href={href}
      {...(external ? { target: "_blank", rel: "noopener noreferrer" } : {})}
    >
      {children}
      {external && <Icon name="arrowUpRight" size={11} />}
      {external && <span className="visually-hidden">(opens in a new tab)</span>}
    </a>
  );
}

export default function Footer() {
  return (
    <footer className="footer">
      <div className="wrap footer__top">
        <div className="footer__brand">
          <a className="brand" href="#top" aria-label="Mesh home">
            <Mark size={24} />
            <span className="brand__word">mesh</span>
          </a>
          <p className="footer__desc">Shared memory, tasks, and changelogs for AI agents. Local-first, MIT.</p>
          <span className="status-line">
            <span className="status-dot presence-dot" aria-hidden="true" />
            local engine / no external daemon
          </span>
        </div>

        {columns.map((column) => (
          <nav aria-label={column.title} key={column.title}>
            <div className="footer__col-title">{column.title}</div>
            <div className="footer__links">
              {column.links.map(([label, href]) => (
                <FooterLink href={href} key={label}>{label}</FooterLink>
              ))}
            </div>
          </nav>
        ))}
      </div>

      <div className="wrap footer__bottom">
        <div className="footer__bottom-group">
          <span>© 2026 Mesh</span>
          <span>·</span>
          <span>MIT</span>
          <span>·</span>
          <span>local-first</span>
        </div>
        <a className="footer__top-link" href="#top">
          Back to top
          <Icon name="arrowDown" size={12} className="footer__up" />
        </a>
      </div>

      <div className="footer__word-wrap">
        <FooterWordmark />
      </div>
    </footer>
  );
}
