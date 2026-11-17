import { AGENSI_URL, GITHUB_URL, INSTALL_CMD } from "../data/content";
import { useCopy } from "../hooks/useCopy";
import Button from "./ui/Button";
import CopyButton from "./ui/CopyButton";
import Icon from "./ui/Icon";
import SectionHeader from "./ui/SectionHeader";

const addCmd = 'mesh add "PostgreSQL 16 runs on port 5432" --tags db,prod --type fact';
const askCmd = 'mesh ask "what port does postgres use?" -n 3';
const installCmd = "mesh install --apply";

const created = [
  ["~/.mesh/", "mode 0700 — all data lives here"],
  ["~/.mesh/mesh_<namespace>.db", "SQLite WAL + FTS5 index"],
  ["~/.mesh/audit_<namespace>.db", "content-free audit log"],
  ["~/.mesh/models/", "optional ONNX weights (~90 MB)"],
  ["~/.mesh/auth_token", "local bearer token, mode 0600"],
];

export default function InstallSection() {
  const [, copy] = useCopy();

  return (
    <section className="section" id="install" aria-labelledby="install-title">
      <div className="wrap">
        <SectionHeader
          index="08"
          route="GET STARTED"
          id="install-title"
          title="Up and running in four commands."
          lede="Install, set up, connect your agents, then teach it something. Everything stays local. Nothing is uploaded. No API key required."
          meta="no API key / no GPU / no daemon required"
        />

        <div className="install__grid">
          <div className="install-shell">
            <div className="install-shell__bar">terminal / install</div>
            <div className="install-shell__body">
              <div className="install-step">
                <span className="install-step__num">01</span>
                <div>
                  <div className="install-step__label">Install the package</div>
                  <div className="install-step__cmd"><span>$ </span>{INSTALL_CMD}</div>
                </div>
                <CopyButton text={INSTALL_CMD} compact label="Copy install command" />
              </div>

              <div className="install-step">
                <span className="install-step__num">02</span>
                <div>
                  <div className="install-step__label">Run setup (optional but recommended)</div>
                  <div className="install-step__cmd"><span>$ </span>mesh setup</div>
                  <p className="install-step__note">
                    Asks y/n before downloading the ~90 MB ONNX model, starting the daemon,
                    or registering login auto-start. All steps are individually consent-gated.
                    Works fully offline with the built-in hash-384 fallback.
                  </p>
                </div>
                <CopyButton text="mesh setup" compact label="Copy setup command" />
              </div>

              <div className="install-step">
                <span className="install-step__num">03</span>
                <div>
                  <div className="install-step__label">Connect your agents</div>
                  <div className="install-step__cmd"><span>$ </span>mesh install --dry-run</div>
                  <div className="install-step__cmd"><span>$ </span>{installCmd}</div>
                  <p className="install-step__note">
                    Writes MCP config, a skill, and a rules block per agent. Creates
                    timestamped backups. Preserves unrelated config.
                    Supports Claude Code, Cursor, Windsurf, Codex, and more.
                  </p>
                </div>
                <CopyButton text={`mesh install --dry-run\n${installCmd}`} compact label="Copy install commands" />
              </div>

              <div className="install-step">
                <span className="install-step__num">04</span>
                <div>
                  <div className="install-step__label">Teach it something and ask</div>
                  <div className="install-step__cmd"><span>$ </span>{addCmd}</div>
                  <div className="install-step__cmd"><span>$ </span>{askCmd}</div>
                </div>
                <CopyButton
                  text={`${addCmd}\n${askCmd}`}
                  compact
                  label="Copy commands"
                />
              </div>
            </div>
          </div>

          <div className="created">
            <div className="created__head">what gets created</div>
            <div className="created__list">
              {created.map(([path, note]) => (
                <div className="created__item" key={path}>
                  <Icon name="check" size={13} />
                  <span>
                    <code>{path}</code> — {note}
                  </span>
                </div>
              ))}
            </div>
            <p className="created__empty">No silent background daemons or downloads. Everything is consent-gated.</p>

            <div className="created__head" style={{ marginTop: "1.25rem" }}>next steps</div>
            <div className="created__list">
              <div className="created__item">
                <Icon name="check" size={13} />
                <span><code>mesh brief</code> — session start summary with open tasks and key facts</span>
              </div>
              <div className="created__item">
                <Icon name="check" size={13} />
                <span><code>mesh task add "..." --kind goal</code> — track work across sessions</span>
              </div>
              <div className="created__item">
                <Icon name="check" size={13} />
                <span><code>mesh add "..." --secret-ref env://KEY</code> — store secret pointers</span>
              </div>
              <div className="created__item">
                <Icon name="check" size={13} />
                <span><code>mesh log --since 7d</code> — project changelog</span>
              </div>
              <div className="created__item">
                <Icon name="check" size={13} />
                <span><code>mesh doctor</code> — verify everything is working</span>
              </div>
            </div>
          </div>
        </div>

        <div className="install__actions">
          <Button icon="copy" onClick={() => copy(INSTALL_CMD, "Install command copied")}>
            Copy install command
          </Button>
          <Button href={GITHUB_URL} variant="secondary" iconRight="arrowUpRight">
            View on GitHub
          </Button>
          <Button href={AGENSI_URL} variant="quiet" iconRight="arrowUpRight">
            Use on Agensi
          </Button>
        </div>
        <p className="install__meta">Python 3.10+ · MIT · v0.8.3 · 17 MCP tools · local-first</p>
      </div>
    </section>
  );
}
