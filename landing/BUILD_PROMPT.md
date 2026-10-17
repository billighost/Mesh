# Mesh landing page — production build brief

You are building the production landing page for **Mesh** (`mesh-context-layer`), a
high-speed shared semantic and hybrid memory layer for AI agents.

This brief is the source of truth. If anything here conflicts with your own taste, the
brief wins. If something is genuinely missing, choose the simplest option that fits the
Kernel design language and note the decision in your handoff report. Do not invent a new
visual direction.

## Read these first, in this order

1. `landing/direction-sampler.html` — the approved interactive preview. Focus on the
   `data-feel="kernel"` tokens and components (lines ~14–58, ~357–660, ~1509–1580,
   ~1755–1785, ~2262–2390). Ignore Atlas, Signal, and Drift completely.
2. `landing/assets/brand/README.md` — the approved mark, lockups, icons, colors, and
   usage rules.
3. `README.md` — the real product behavior and exact CLI/MCP/HTTP/SDK examples.
4. `pyproject.toml` — version, entry points, Python requirement, license.
5. `https://www.agensi.io/skills/mesh-context-layer` — the distribution listing. Use it
   for the Agensi section, the "Free / Security scanned / 27 agents" claims, and the
   example session. **Do not use the Agensi listing's logo image.** The user explicitly
   rejected it. Use the three-node mark from `landing/assets/brand/`.

## Non-negotiables

- **Direction: Kernel only.** Terminal precision, white-first, acid lime, hard offset
  shadows, 1–2px radii, monospaced display type. Do not blend in Atlas, Signal, or Drift.
- **White is the major color.** Light mode is the default and the primary design. Dark
  mode is a full second palette, not a CSS inversion.
- **Use the approved mark.** The three-node glyph from the preview / `assets/brand/`.
  Never the Agensi logo, never a third-party agent logo, never a redrawn mark.
- **React + JavaScript + plain CSS.** Vite, JSX, CSS custom properties. No TypeScript,
  Tailwind, CSS-in-JS, UI kits, animation libraries, or icon libraries.
- **Motion is a feature, not decoration.** The page must feel alive, but every animation
  must be purposeful, skippable, and disabled by `prefers-reduced-motion` / the Motion
  toggle.
- **Every interactive demo must work with the keyboard and without motion.** No
  cursor-only affordances. No auto-playing media. No scroll-jacking.
- **No invented claims.** Use only the product facts in section 1. No testimonials,
  customer logos, star counts, fake benchmarks, "trusted by", or "production-grade"
  puffery.
- **No Agensi logo.** Text "Agensi" and a link is correct. The listing's product-media
  image is banned.
- The deliverable is a real Vite app in `landing/site/`. Do not modify
  `direction-sampler.html`, `assets/brand/`, or any Python file in the repo.

---

# 1. Product truth (use only these claims)

**One-line:** High-speed shared semantic and hybrid memory layer for AI agents.

**Story:** When one agent learns something, every other agent can recall it — by
meaning, keyword, or exact identifier. Every agent starts each session with amnesia;
Mesh gives the whole stack one persistent memory.

**The five retrieval stages:**
1. Dense vector SIMD cosine (`cache.matrix @ q_vec`) — documented at under 0.1 ms.
2. SQLite FTS5 BM25 keyword relevance.
3. Robertson–Spärck Jones smoothed IDF.
4. Domain synonyms.
5. Exact token overlap.

**Storage:** pure SQLite WAL per namespace, `~/.mesh/`, permissions `0700`, no external
database daemon.

**Memory behavior:**
- Identical facts are reinforced in place (dedupe).
- Contradictory facts are flagged for review; no silent data loss.
- TTL confidence decay (`--ttl <days>` / `--clear-ttl`).
- Explicit consent before background daemon, OS login auto-start, ~90 MB ONNX model
  download, or cloud log sync.
- `local_only` private memories: excluded from export; redacted in audit logs,
  contradictions, digest, and conflict warnings; excluded from recall/inspect by
  default unless `include_private=true`.
- Secrets are auto-redacted and forced private.
- Namespace isolation; `MESH_LOCKED_NAMESPACE` can confine a server to one namespace.
- `MESH_ENFORCE_AGENT_OWNERSHIP=1` prevents agents from editing each other's memories.

**Embeddings:** local `all-MiniLM-L6-v2` ONNX, 384 dimensions, optional ~90 MB download
into `~/.mesh/models/`. If declined or non-interactive, a built-in 384-dimensional
feature-hash engine (`hash-384`) serves requests in under 0.05 ms; vectors auto-heal
later if the ONNX model is downloaded. No GPU and no API key required for local use.

**Interfaces:**
- MCP server with 10 tools (`mesh_learn`, `mesh_recall`, `mesh_update`, `mesh_forget`,
  `mesh_inspect`, `mesh_context`, `mesh_patterns`, `mesh_contradictions`,
  `mesh_resolve_contradiction`, `mesh_sync`).
- HTTP REST API (`mesh-http --port 7701`).
- CLI (`mesh`), Python SDK (`from mesh import Mesh`), web dashboard
  (`mesh-dashboard`), bundled agent skill (`mesh install-skills`).
- Python 3.10+, MIT, package `mesh-context-layer` v0.6.3.

**Agensi listing (attribute these, do not overclaim):**
- Free.
- Security scanned.
- Works with 27 agents.
- Named on the listing: Claude Code, Cursor, Codex CLI, Muse, Dots, OpenClaw, Manus,
  Grok, Grok Bot, +18 more.
- Example session: staging database moved from port 5432 to 5439; memory `db-cfg-92`
  updated; conflict `conf-412` resolved.
- CTA label on the listing: "Use this skill".

**Links:**
- GitHub: `https://github.com/billighost/mesh`
- PyPI: `https://pypi.org/project/mesh-context-layer/`
- Changelog: `https://github.com/billighost/mesh/blob/main/CHANGELOG.md`
- Agensi: `https://www.agensi.io/skills/mesh-context-layer`

**Numbers you may display:** `5` stages, `384` dimensions, `10` MCP tools, `27` agents
via Agensi, `<0.1 ms` dense cosine, `<0.05 ms` hash-384 fallback, `0700` permissions,
`v0.6.3`, `Python 3.10+`, `MIT`, `~90 MB` ONNX model, `0` external database daemons.
Do not invent others.

**Demo data:** the 0.93 similarity, 0.08 ms, 3 agents, and stage scores are illustrative
example data. Label demos "example data" / "simulated example" and never present them as
a benchmark.

---

# 2. The Kernel design system

## 2.1 Tokens

Put these in `src/styles/tokens.css`. Use the exact values. Tokens are the only place
colors are defined; components reference variables, never raw hex values.

```css
:root {
  color-scheme: light;

  --bg: #F7F8F3;
  --surface: #FFFFFF;
  --surface-2: #EEF0E7;
  --ink: #0B0D08;
  --muted: #5D6455;
  --line: #D7DBCD;
  --accent: #B9F24A;
  --accent-ink: #0B0D08;
  --accent-text: #2F6B00;
  --accent-soft: #E8F9C7;
  --warn: #B45309;
  --warn-soft: #FEF3C7;

  --font-display: "Martian Mono", ui-monospace, "SFMono-Regular", monospace;
  --font-body: "Instrument Sans", "Segoe UI", system-ui, sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, monospace;

  --radius: 2px;
  --radius-lg: 6px;
  --btn-radius: 2px;
  --border-w: 1px;
  --shadow: 4px 4px 0 var(--ink);
  --shadow-hover: 5px 5px 0 var(--ink);
  --shadow-press: 1px 1px 0 var(--ink);
  --shadow-soft: none;
  --ease: cubic-bezier(.2, .8, .2, 1);
  --ease-out: cubic-bezier(.16, 1, .3, 1);

  --max: 1240px;
  --gutter: clamp(20px, 4vw, 40px);
  --section-y: clamp(72px, 9vw, 132px);
  --topbar-h: 64px;

  --terminal-bg: #0B0D08;
  --terminal-ink: #F4F7EC;
  --terminal-dim: #A2AA96;
  --terminal-line: #2A2F22;

  --focus: var(--accent-text);
  --selection-bg: var(--accent);
  --selection-fg: var(--accent-ink);
}

:root[data-theme="dark"] {
  color-scheme: dark;

  --bg: #0B0D08;
  --surface: #11140C;
  --surface-2: #171B11;
  --ink: #F4F7EC;
  --muted: #A2AA96;
  --line: #2A2F22;
  --accent: #C8F04A;
  --accent-ink: #0B0D08;
  --accent-text: #C8F04A;
  --accent-soft: #1E2A0E;
  --warn: #FBBF24;
  --warn-soft: #2A230B;

  --terminal-bg: #11140C;
  --terminal-ink: #F4F7EC;
  --terminal-dim: #A2AA96;
  --terminal-line: #2A2F22;

  --focus: var(--accent);
}
```

Rules:
- Never use `#000` or `#FFF` as a page background. Use the tokens.
- `--accent` is a fill/signal color. For lime-colored *text* on a light surface, use
  `--accent-text` (#2F6B00). In dark mode `--accent-text` is the lime itself.
- Shadows are hard offsets, not blurs. No glow, no glassmorphism, no gradients as primary
  surfaces. Gradients are allowed only as faint background fields behind the hero.
- Radii: cards/panels 2–6px; buttons 2px; status pills may be fully rounded; nothing else.

## 2.2 Fonts

Load exactly these families from Google Fonts in `index.html`:

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400..700&family=JetBrains+Mono:wght@400;500;700&family=Martian+Mono:wght@400..800&display=swap" rel="stylesheet">
```

- **Display:** Martian Mono, weight 600 (occasionally 500/700). This is the signature
  face. Headings, numerals, the hero, the wordmark, section titles. It is wide: use
  `max-width` in `ch` units and reduce letter-spacing to `-0.03em` / `-0.04em`.
- **Body:** Instrument Sans 400/500/600. Paragraphs, buttons, labels, UI.
- **Mono:** JetBrains Mono 400/500. Kickers, metadata, badges, terminal, code.
- Do not add a fourth family. Do not use Martian Mono for paragraphs longer than a
  label or a short pull quote.
- Fallbacks: `ui-monospace` and `system-ui`; the page must remain readable if fonts fail.

Type scale:

| Role | Size | Line height | Notes |
| --- | --- | --- | --- |
| Hero h1 | `clamp(2.5rem, 5.6vw, 5rem)` | 1.02 | Martian Mono 600, `letter-spacing: -0.04em`, `max-width: 15ch` |
| Section h2 | `clamp(2rem, 3.8vw, 3.4rem)` | 1.05 | Martian Mono 600, `-0.035em`, `max-width: 18ch` |
| Section h3 | `clamp(1.2rem, 1.7vw, 1.5rem)` | 1.2 | Martian Mono 600 or Instrument Sans 600 for card titles |
| Body large | `clamp(1rem, 1.3vw, 1.15rem)` | 1.65 | Instrument Sans 400, `max-width: 58ch` |
| Body | 16px | 1.65 | Instrument Sans 400 |
| Small | 14px | 1.55 | Instrument Sans 400 |
| Mono label | 10–11px | 1.4 | JetBrains Mono 500, uppercase, `letter-spacing: .12em` |
| Mono body | 12–13px | 1.75 | JetBrains Mono 400 |
| Huge numeral | `clamp(3rem, 8vw, 7rem)` | 0.9 | Martian Mono 600, `font-variant-numeric: tabular-nums` |

## 2.3 Layout

- `.wrap { width: min(var(--max), 100% - (2 * var(--gutter))); margin-inline: auto; }`
- Sticky topbar, 64px tall, solid `--bg` (not translucent), 1px bottom border.
- Sections: `padding-block: var(--section-y); border-top: 1px solid var(--line);`
  The hero has no top border. Use a max-width 1240px content column.
- Section header pattern:
  - A mono index label (`01 / THE PROBLEM`) above or left of the h2.
  - h2 + one short lede, max 58ch.
  - Optional right-side meta (a badge, a mono note, a count).
  - On desktop, a 2-column header (`minmax(0, 1fr) minmax(0, 1.4fr)`) is allowed;
    on mobile, stack.
- Vertical rhythm inside sections: 24/32/48px. Do not use uniform 16px gaps everywhere;
  the page needs deliberate breathing room.
- Faint grid field behind the hero and selected panels:
  ```css
  .grid-field::before {
    content: "";
    position: absolute; inset: 0; pointer-events: none;
    background-image:
      linear-gradient(var(--line) 1px, transparent 1px),
      linear-gradient(90deg, var(--line) 1px, transparent 1px);
    background-size: 32px 32px;
    opacity: .35;
    mask-image: radial-gradient(ellipse at 50% 30%, #000 30%, transparent 75%);
  }
  ```
  Use it once or twice, not on every section.
- Corner ticks: optional 6px bracket marks on large panels, using pseudo-elements with
  1px `--line`. Keep them subtle.
- Avoid giant empty areas. Every section must earn its height.

## 2.4 Core components

Build these in `src/components/ui/`:

**Button**
- `.btn`: Instrument Sans 600, 14px, 12px 18px, 1px transparent border, 2px radius,
  inline-flex, 8px gap, min-height 44px.
- Primary: `background: var(--accent); color: var(--accent-ink); border-color: var(--ink); box-shadow: var(--shadow);`
- Secondary: transparent background, `color: var(--ink)`, `border-color: var(--line)`.
- Quiet: transparent, no border, no shadow, `color: var(--muted)`, padding-inline 8px.
- Icon: 42×42, surface background, 1px line border.
- Hover (fine pointer only): `translate(-1px,-1px)`, `box-shadow: var(--shadow-hover)`.
- Active: `translate(2px,2px)`, `box-shadow: var(--shadow-press)`.
- Disabled: opacity .45, cursor not-allowed, no transform.
- Focus-visible: `outline: 2px solid var(--focus); outline-offset: 2px;`
- Primary buttons may have a subtle magnetic pull (max 4px) on fine pointers when motion
  is on. Do not apply it to nav links or form fields.

**Badge**
- `font-family: var(--font-mono); font-size: 10px; text-transform: uppercase; letter-spacing: .08em;`
- 1px `--line` border, 2px radius, padding 4px 7px, `background: var(--surface)`.
- Accent variant: `background: var(--accent); color: var(--accent-ink); border-color: var(--accent);`
- Solid variant: ink background, background-color text. Use sparingly.

**Panel / Card**
- `background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius-lg);`
- Optional hard shadow for hero/demo panels: `box-shadow: var(--shadow); border-color: var(--ink);`
- Hover lift only on interactive cards: `translate(-2px,-2px)` + `--shadow`.
- Do not put a shadow on every card; it loses meaning.

**Terminal**
- An ink object in both themes: `background: var(--terminal-bg); color: var(--terminal-ink);`
  `border: 1px solid var(--ink); border-radius: var(--radius-lg); box-shadow: var(--shadow);`
  In dark mode use `border-color: var(--line); box-shadow: 4px 4px 0 #000;`
- Bar: 38px, 1px bottom border, 6px dots (first lime, others `--terminal-line`), mono 10px
  label, right-aligned "live" in lime.
- Body: JetBrains Mono 12px, line-height 1.8, padding 16px 18px.
- `.dim` = `--terminal-dim`; `.ok` = `--accent`; cursor 7×13 lime block with
  `steps(2, start)` blink.
- The terminal must be readable at 360px; allow horizontal scroll only inside the body
  if a line cannot wrap, but prefer wrapping with `overflow-wrap: anywhere`.

**Code block**
- Same ink shell as terminal, top bar with language label and a copy button.
- JetBrains Mono 12.5px, line-height 1.7, padding 18px, `tab-size: 2`.
- Horizontal scroll inside the block on mobile; never let the page scroll horizontally.
- Syntax colors: commands/keys lime, strings `--terminal-ink`, comments
  `--terminal-dim`, punctuation `--terminal-dim`. No rainbow highlighting.

**Segmented control**
- 3-state theme control (System / Light / Dark) and 2-state demo controls.
- 1px line border, 2px radius, 2px padding; segments 32px tall; active segment gets
  `background: var(--surface-2)` and `box-shadow: inset 0 0 0 1px var(--line)`.
- `role="radiogroup"` or `role="tablist"` as appropriate; arrow-key navigation.

**Switch**
- 28×16 track, 2px padding, 12px thumb, 2px radius; on = `--accent` track + `--accent-ink`
  thumb; off = `--line` track + `--surface` thumb. `role="switch"`, `aria-checked`.
  Label is always visible or programmatically associated; never icon-only without a label.

**Tabs**
- `role="tablist"`, `role="tab"`, `role="tabpanel"`; arrow keys move focus and selection;
  `aria-selected`; active tab gets a lime underline or lime left border (not a pill).
  Tabs scroll horizontally on mobile with `overflow-x: auto` and visible focus.

**Accordion**
- Button + panel; `aria-expanded`, `aria-controls`; one item open at a time; animate
  opacity/translate, not height, or use a measured max-height. First item may start open.
  Do not use `<details>` unless you style the marker reliably.

**Toast**
- Fixed bottom center, max-width 420px, ink background, 1px lime border, 2px radius,
  mono 12px, `role="status"`, `aria-live="polite"`; auto-dismiss 2.6s; no layout shift.
- Copy failures fall back to a visible text field/selection, never `alert`.

**Reveal**
- `IntersectionObserver`, threshold 0.15, once. Apply `.is-in` when visible. Children get
  `transition-delay` via `--i`. If motion is off, add `.is-in` immediately (or omit the
  wrapper effect). Never hide content that fails JS; use a `.js` class on `<html>` before
  applying hidden states, or default to visible and animate in only when observed.

## 2.5 Motion

Motion language: **precise, mechanical, quick**. Not bouncy, not floaty. Think relay
switches, packets, scanlines, counters, typed terminals, marquees.

- Easing: `var(--ease)` for UI, `var(--ease-out)` for entrances.
- Durations: 120–220ms for hover/press; 400–700ms for reveals; 900–1800ms for demo
  sequences. Never longer than 2s for a functional transition.
- Hero entrance: kicker (0ms), h1 line 1 (60ms), h1 line 2 (120ms), lede (180ms), CTAs
  (240ms), terminal (300ms), flow (380ms). Use opacity + translateY(12px), not scale.
- Section reveals: opacity 0→1, translateY(14px)→0, 600ms, stagger 60ms.
- Terminal: reveal lines 70ms apart on first view; a small "replay" icon button in the
  bar. Do not type character-by-character for the whole hero on every visit; it is slow
  and annoying. A single 200ms scramble on the hero kicker is enough.
- Demo packets: a 6px lime square travels along a straight path over 700ms with
  `cubic-bezier(.4,0,.2,1)`. Use CSS transforms; clean up timers.
- Marquee: 28s linear infinite; pause on hover/focus; duplicate content with
  `aria-hidden` on the duplicate; stop under reduced motion.
- Cursor spotlight: only on fine pointers and motion on; radial-gradient follows
  `--mx/--my` on the How-it-works panel; keep opacity ≤ .18.
- Scroll progress: 2px lime bar under the topbar; use `requestAnimationFrame` and
  passive scroll listener; width from 0 to 100%.
- Hover: translate + shadow, 160ms. Do not scale text.
- Numbers: animate 0→target with `requestAnimationFrame` (800ms, easeOutCubic) only for
  integer facts (5, 384, 10, 27). Do not animate `<0.1`, `0700`, or dates.
- **Reduced motion / Motion off:** `* { animation-duration: .01ms !important;
  animation-iteration-count: 1 !important; transition-duration: .01ms !important;
  scroll-behavior: auto !important; }` plus explicit logic: no auto-play, no marquee, no
  typing, no packet, no counters, no spotlight. Show final states immediately.

## 2.6 Theme and motion boot

`index.html` runs this inline before the app renders to avoid a flash:

```html
<script>
  (function () {
    try {
      var root = document.documentElement;
      var pref = localStorage.getItem("mesh-theme");
      if (pref !== "light" && pref !== "dark") pref = "system";
      var dark = pref === "system"
        ? window.matchMedia("(prefers-color-scheme: dark)").matches
        : pref === "dark";
      root.dataset.theme = dark ? "dark" : "light";
      root.dataset.themePref = pref;
      root.style.colorScheme = dark ? "dark" : "light";

      var motion = localStorage.getItem("mesh-motion");
      if (motion !== "on" && motion !== "off") {
        motion = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "off" : "on";
      }
      root.dataset.motion = motion;
    } catch (e) { /* keep CSS defaults */ }
  })();
</script>
```

React state must read the attributes on mount, not assume defaults. Listen to
`matchMedia("(prefers-color-scheme: dark)")` while the preference is `system`. Persist
only the preference (`system|light|dark`) under `mesh-theme`, and `on|off` under
`mesh-motion`. Set `data-theme` to the **resolved** light/dark value; set
`data-theme-pref` to the preference. Set `data-motion` on `<html>`.

The topbar theme control is a 3-segment control with visible text on desktop and icons +
accessible labels on mobile. The Motion control is a switch labeled "Motion". Put both in
the mobile menu on small screens.

---

# 3. Page architecture

Order is intentional. It tells the story: amnesia → shared memory → retrieval → trust →
engine → interfaces → distribution → install → objections → close.

1. **Topbar** — logo, nav, theme, motion, install.
2. **Hero** — the promise + live terminal + agent flow.
3. **Trust strip** — Agensi / security scanned / 27 agents / MIT + agent marquee.
4. **01 The problem** — "Every session starts from zero." Without/with toggle.
5. **02 How it works** — interactive Learn → Index → Recall story.
6. **03 The retrieval engine** — five-stage hybrid search explorer.
7. **04 Memory that behaves** — dedupe / contradictions / TTL / privacy tabs.
8. **05 Under the hood** — stats, architecture diagram, spec sheet, doctor card.
9. **06 Interfaces** — 27 agents + Python / MCP / CLI / HTTP tabs.
10. **07 Distribution** — Agensi card + example session.
11. **08 Get started** — three commands + what gets created.
12. **09 FAQ** — six objections.
13. **Final CTA + footer** — one last promise + huge outlined wordmark.

Desktop wireframe (not to scale):

```
┌──────────────────────────────────────────────────────────────┐
│ mesh   How it works  Search  Memory  Interfaces  Agensi      │
│                                    [SYS|LGT|DRK] [Motion] [Install] │
├──────────────────────────────────────────────────────────────┤
│ KICKER                                                       │
│ Your agents are brilliant.          ┌──────────────────────┐ │
│ And they have amnesia.              │ mesh / checkout      │ │
│                                     │ $ mesh status        │ │
│ One shared memory...                │ ok local / sqlite... │ │
│ [Install mesh] [See it work]        │ ...                  │ │
│ MIT / local-first / Python 3.10+    └──────────────────────┘ │
│                                     backend → mesh → frontend│
├──────────────────────────────────────────────────────────────┤
│ FREE ON AGENSI  ·  SECURITY SCANNED  ·  27 AGENTS  ·  MIT    │
│ CLAUDE CODE · CURSOR · CODEX CLI · ... (marquee)             │
├──────────────────────────────────────────────────────────────┤
│ 01 / THE PROBLEM                                             │
│ Every session starts from zero.                              │
│ [Without mesh | With mesh]        [illustrative diagram]     │
├──────────────────────────────────────────────────────────────┤
│ 02 / HOW IT WORKS                                            │
│ One agent learns. Every agent knows.                         │
│ [01 Learn] [02 Index] [03 Recall]          [Run the story]   │
│ agent A ───── mesh ───── agent B                             │
│ [what A learned]              [what B recalled]              │
├──────────────────────────────────────────────────────────────┤
│ 03 / THE RETRIEVAL ENGINE                                    │
│ One query. Five independent signals.                         │
│ [query input.................] [Run hybrid search]           │
│ 01 dense ... 02 bm25 ... 03 idf ... 04 synonyms ... 05 exact  │
│ [fused confidence 0.93]       [recalled fact card]           │
└──────────────────────────────────────────────────────────────┘
```

Mobile: single column, copy first, terminal/demos second, all controls full-width,
tabs horizontally scrollable, diagrams stack. No horizontal page scroll at 360px.

---

# 4. Section-by-section spec and copy deck

Use the copy exactly unless a change is explicitly allowed. Do not add extra marketing
paragraphs. Keep the page tight.

## 4.1 Topbar

- Left: currentColor three-node mark (from `mesh-mark-currentColor.svg` / inline React
  component) + wordmark "mesh" in Martian Mono 600, 18px. Link to `#top`, aria-label
  "Mesh home".
- Center/right nav links: **How it works** → `#how`, **Search** → `#search`, **Memory** →
  `#memory`, **Interfaces** → `#interfaces`, **Agensi** → `#agensi`. Underline on hover;
  no pill backgrounds.
- Right: 3-segment theme control; Motion switch; primary **Install** button that copies
  `pip install mesh-context-layer` and shows a toast. On desktop, add a mono `v0.6.3`
  badge next to the logo or before the controls.
- Sticky with a 1px bottom border and a 2px lime scroll-progress bar at the very top.
- Mobile (< 900px): logo + menu button. The menu is a full-width panel under the topbar
  with the nav links, theme control, motion switch, install button, and external links.
  Focus trap, Escape to close, close on link click, `aria-expanded`, `aria-controls`.
  Lock body scroll while open. Restore focus to the menu button.

## 4.2 Hero

Kicker (mono): `Shared memory for AI agents`

H1 (Martian Mono 600, two lines):
```
Your agents are brilliant.
And they have amnesia.
```
Visually emphasize "amnesia": a lime highlight block behind the word (use
`background: var(--accent); color: var(--accent-ink); padding: 0 .08em;` with a slight
negative margin) or a thick lime underline. Do not animate the highlight on loop.

Lede:
> Mesh gives every agent one shared memory. One learns a fact. Every other agent recalls
> it in under a tenth of a millisecond.

CTAs:
- Primary: **Install mesh** — copies `pip install mesh-context-layer`, toast
  "Install command copied".
- Secondary: **See it work** — smooth-scrolls to `#how`.
- Quiet link: **Read the docs ↗** — GitHub, external.

Meta row (mono 10.5px): `MIT` · `local-first` · `Python 3.10+` · `no external database`.
Add a small lime dot before the first item? Keep it as text separators `/`.

Right visual: a **Terminal** card with the example session, plus a flow row beneath and a
caption.

Terminal bar: first dot lime; label `mesh / checkout-service`; right `live`.

Terminal body:
```
$ mesh status
ok  local / sqlite wal / 384d
$ backend.learn("postgres runs on :5432")
ok  indexed / dedupe on / 0.08 ms
$ frontend.recall("postgres port")
ok  1 hit / 0.93 sim / backend-agent
ok  shared with 3 agents
ok  _
```
The `_` is the blinking cursor. Reveal lines on load (70ms stagger) and provide a replay
icon button in the bar. If motion off, show all lines.

Flow row beneath: `backend-agent` → `mesh` → `frontend-agent` in small bordered chips;
the mesh chip is hot (lime border/text). Use `learn` and `recall` as the tiny arrow
labels, or a simple arrow with an accessible label. Caption: `example session / local only`.

Background: faint grid field + a lime radial glow behind the terminal (opacity ≤ .5),
masked. Keep the hero on `--bg`, not a giant colored panel. White must dominate.

## 4.3 Trust strip

A full-width band with `background: var(--surface); border-block: 1px solid var(--line);`
Inner wrap:
- Left: mono label `FREE ON AGENSI` + small lime dot, link to the Agensi listing.
- Middle: badges `Security scanned`, `27 agents`, `MIT`.
- Right (desktop): mono `v0.6.3` / `Python 3.10+`.
Below, a one-line marquee in mono 11px uppercase, separated by `·`:
```
CLAUDE CODE · CURSOR · CODEX CLI · MUSE · DOTS · OPENCLAW · MANUS · GROK · GROK BOT · +18 MORE
```
Duplicate the line for the loop, `aria-hidden` on the duplicate, pause on hover/focus,
mask the edges with `mask-image: linear-gradient(90deg, transparent, #000 8%, #000 92%, transparent)`.
The accessible label should read the agent names once. No logos.

## 4.4 01 / The problem — "Every session starts from zero."

Kicker: `01 / THE PROBLEM`
H2: `Every session starts from zero.`
Lede:
> Your terminal agent discovers the staging port. Your editor agent never hears about it.
> Your review agent re-learns the same constraint for the third time this week. The work is
> already done — it just isn't anywhere your next agent can reach.

Interactive: a segmented control **Without mesh** / **With mesh**, default **Without
mesh**.

Without mesh panel:
- Three agent cards in a row: `claude / terminal`, `cursor / editor`, `codex / review`.
- Each card contains the same fact: `Production PostgreSQL 16 runs on port 5432.`
- Each card has a badge `learned again` and a duplicated-fact count `copy 1/2/3`.
- Dashed, broken lines between cards; a small `×` at each break. Use `--warn` for the
  break marks and `--warn-soft` for the badges.
- Tally at the bottom: `same fact / 3 copies / 0 shared`.

With mesh panel:
- A central hub card labeled `mesh / checkout-service` with the fact and a lime border.
- Three agent cards around/below it, connected by solid lime lines with check marks.
- Each agent badge says `recalls`; the hub says `1 shared fact`.
- Tally: `same fact / 1 copy / 3 agents`.
- The agents should visibly point at the hub; no agent-to-agent lines.

Transition: crossfade + 8px slide between panels; keep a fixed min-height so the page
doesn't jump. If motion off, switch instantly. Label the visual `illustrative diagram`.

Pull quote below (bordered left with 3px lime):
> The amnesia tax is paid in tokens, latency, and repeated mistakes.

Keep it one line on desktop, wrap on mobile.

## 4.5 02 / How it works — "One agent learns. Every agent knows."

Kicker: `02 / HOW IT WORKS`
H2: `One agent learns. Every agent knows.`
Lede:
> Run the story. Agent A learns a production fact. Mesh embeds it, deduplicates it, and
> indexes it five ways. Agent B asks a different question and still gets the answer —
> without ever talking to Agent A.

Demo panel (bordered, hard shadow):
- Top bar: left `Live story` + badge `example data`; center/right step chips
  `01 Learn`, `02 Index`, `03 Recall`; buttons `Run the story`, and a secondary
  `Reset` (the manual `Agent A learns` / `Agent B recalls` buttons can live below).
- Stage: three nodes.
  - Left: `agent a` / `backend-agent` / verb `learns`.
  - Center: hub `shared` / `mesh` / `checkout-service`, with a small three-node mark.
  - Right: `agent b` / `frontend-agent` / verb `recalls`.
  - Connect left→hub and hub→right with 1px dashed lines; active segments become solid
    lime. A 6px lime packet travels on active segments.
  - During Index (step 2), show five tiny labels under the hub:
    `dense · bm25 · idf · synonyms · exact`, lighting up sequentially.
- Readout: two cards.
  - Left heading `What backend-agent learned`.
    Empty: `Nothing learned yet. Run the story to teach backend-agent a fact.`
    Filled: fact `Production PostgreSQL 16 runs on port 5432 with pgBouncer connection
    pooling.` + badges `fact`, `db / prod`, `384d`.
  - Right heading `What frontend-agent recalled`.
    Empty: `frontend-agent has not asked yet. It will recall the fact without ever
    talking to backend-agent.`
    Filled: query `what port does postgres use?`, the same fact, badges
    `0.93 similarity`, `from backend-agent`, `<0.1 ms`.
- Controls below: `Run the story` (primary), `Agent A learns` (secondary), `Agent B
  recalls` (secondary), `Reset` (quiet). Run sequence: step 1 at 0ms, step 2 at 900ms,
  step 3 at 1800ms. Manual buttons set the relevant step immediately.
- Status note (`aria-live="polite"`):
  - step 0: `One SQLite file per namespace. No external database daemon.`
  - step 1: `learn / embed 384d / dedupe / hybrid index: dense + BM25 + IDF + synonyms + exact.`
  - step 2: `index / five independent signals, fused into one ranked result.`
  - step 3: `recall / five-stage hybrid search / sanitized context for frontend-agent.`
- Caption: `example data / local only`.

Auto-play once when the panel first enters the viewport, only if motion is on and the OS
does not prefer reduced motion. Cancel on any manual interaction. Store a ref so it never
auto-plays twice in a session. Clean up all timers.

## 4.6 03 / The retrieval engine — "One query. Five independent signals."

Kicker: `03 / THE RETRIEVAL ENGINE`
H2: `One query. Five independent signals.`
Lede:
> Most memory tools pick one trick. Mesh runs five retrieval stages and fuses the
> evidence. That's how it finds a fact when the wording is different, the keyword is
> rare, or the identifier is exact.

Demo panel, two columns on desktop (left controls + stages, right result):

Left:
- Label `Query`, input `id="mesh-query"`, placeholder `Ask the mesh anything`, prefilled
  with `what port does postgres use?`, autocomplete off.
- Primary `Run hybrid search` button; pressing Enter in the input runs.
- Sample chips (mono, small): `what port does postgres use?`, `staging database port`,
  `JWT RS256`. Clicking a chip fills the input and runs.
- Mode toggle: `Hybrid` / `Vector only`. Default Hybrid.
- Stage list, five rows:
  1. `01 / DENSE VECTOR` — `384d embeddings, SIMD cosine on a local matrix cache.` — tag `<0.1 ms`
  2. `02 / BM25` — `SQLite FTS5 keyword relevance over the namespace.` — tag `FTS5`
  3. `03 / IDF` — `Robertson–Spärck Jones smoothed inverse document frequency.` — tag `rare terms`
  4. `04 / SYNONYMS` — `Domain dictionary bridges postgres ↔ PostgreSQL, port ↔ port number.` — tag `domain-aware`
  5. `05 / EXACT TOKENS` — `Ports, IDs, flags, identifiers. Exact overlap is a signal too.` — tag `exact`
  Each row has a stage name, description, a score (`0.98`), and a 100% width track with a
  lime fill whose width = score × 100%. On run, rows activate sequentially (~120ms).
  In Vector only mode, only row 1 is active; rows 2–5 are dimmed and tagged `skipped`.
- Run state: button label `Running…` while active; progress announced via `aria-live`
  only at completion, not every stage.

Right:
- Result card, heading `recalled fact`.
- Content from the selected sample.
- Meta badges: source agent, tags, `local / 0.08 ms` (example).
- A fusion summary above or below: `FUSED CONFIDENCE` + big score (e.g. `0.93`) +
  `5 / 5 stages` (or `1 / 5 stages` in vector-only) + a mono note:
  - Hybrid: `Five signals. One ranked result.`
  - Vector only: `Dense only. Exact identifiers and rare terms lose their boost.`
- A small note at the bottom of the panel: `Illustrative, deterministic scores. Real
  recall depends on your corpus and embedding model. No network request is made.`

Sample data (use exactly; all scores illustrative):

```js
export const searchSamples = [
  {
    id: "postgres-port",
    query: "what port does postgres use?",
    fact: "Production PostgreSQL 16 runs on port 5432 with pgBouncer connection pooling.",
    source: "backend-agent",
    tags: ["fact", "db / prod", "384d"],
    stages: [
      { key: "dense", score: 0.98 },
      { key: "bm25", score: 0.88 },
      { key: "idf", score: 0.86 },
      { key: "synonyms", score: 0.92 },
      { key: "exact", score: 0.97 },
    ],
    fused: 0.93,
    vectorOnly: 0.84,
    latency: "0.08 ms",
  },
  {
    id: "staging-port",
    query: "staging database port",
    fact: "Staging database port is 5439.",
    source: "devops-agent",
    tags: ["fact", "database / staging", "384d"],
    stages: [
      { key: "dense", score: 0.91 },
      { key: "bm25", score: 0.96 },
      { key: "idf", score: 0.78 },
      { key: "synonyms", score: 0.95 },
      { key: "exact", score: 0.99 },
    ],
    fused: 0.92,
    vectorOnly: 0.81,
    latency: "0.07 ms",
  },
  {
    id: "jwt-rs256",
    query: "JWT RS256",
    fact: "Auth service requires JWT RS256 signatures.",
    source: "security-agent",
    tags: ["instruction", "auth / jwt", "384d"],
    stages: [
      { key: "dense", score: 0.88 },
      { key: "bm25", score: 0.97 },
      { key: "idf", score: 0.91 },
      { key: "synonyms", score: 0.78 },
      { key: "exact", score: 1.0 },
    ],
    fused: 0.9,
    vectorOnly: 0.73,
    latency: "0.06 ms",
  },
];
```

Do not compute `fused` from the stage scores. It is a calibrated fusion output; present
it as example data. Do not show weights, so no one expects a weighted sum. The point is
the *pattern* — five independent signals, one ranked result.

Input validation: if the query is empty or only whitespace, show inline text
`Type a query to run the example.` (muted, not red) and do not run. Do not use `alert`.

## 4.7 04 / Memory that behaves — "Remember more. Trust it more."

Kicker: `04 / MEMORY THAT BEHAVES`
H2: `Remember more. Trust it more.`
Lede:
> A shared memory is only useful if it stays clean. Mesh reinforces what repeats, flags
> what conflicts, ages what goes stale, and keeps private facts private.

Tabs: `Dedupe`, `Contradictions`, `TTL`, `Privacy`. Use a bordered tab strip with a lime
left border or underline on the active tab. Panel min-height so switching doesn't jump.

**Dedupe**
- Visual: two cards containing near-identical facts:
  - `Production PostgreSQL 16 runs on port 5432.`
  - `Production PG 16 runs on :5432.`
- Button `Learn it again`. On click, the second card slides/fades into the first; the
  first gets a `×2` badge and a line `reinforced in place`.
- Copy: `Identical facts are reinforced, not duplicated. The original ID and history stay
  intact.`
- Reset/replay control.

**Contradictions**
- Visual: two cards: `Port 5432` and `Port 5439`, with tags `old` / `new`.
- A warning banner using `--warn` / `--warn-soft`: `CONFLICT DETECTED — review required`.
- Button `Resolve`. On click, the banner becomes `RESOLVED — 5439 is current` with a lime
  check; the old card stays visible with a `superseded` badge.
- Copy: `Contradictory facts are flagged for review. Nothing is silently overwritten, and
  nothing is lost.`
- Reset control.

**TTL**
- Visual: a confidence meter `1.00`, a day counter `day 0`, and a button `Advance 30
  days`. Each click adds 30 days and reduces confidence through the illustrative values
  `1.00 → 0.74 → 0.55 → 0.41`; at < 0.5 show a `stale` badge. A reset link.
- Copy: `Set a TTL when a fact is temporary. Confidence decays toward stale; --clear-ttl
  makes it permanent.`
- Do not imply a real timer is running; this is a manual demonstration.

**Privacy**
- Visual: a memory card with a `local_only` badge and content
  `Production credentials live in Vault.` (fictional example).
- Switch `Include private memories`, default off. Off: content is masked
  `••••••••••••••••`, and a line: `Excluded from recall by default. Redacted in audit,
  conflicts, and digest. Excluded from export.` On: content visible, with a note `Only
  when explicitly requested (include_private=true).`
- A second line: `Secrets are auto-redacted and forced private.`
- Copy: `Private memories stay private unless a caller explicitly asks for them.`

Below the tabs, a 4-card feature grid:
- **Namespaces** — `One SQLite WAL per project. MESH_LOCKED_NAMESPACE can confine a
  server to a single namespace.`
- **Consent** — `y/n before starting a daemon, registering auto-start, downloading the
  model, or syncing logs to a cloud API.`
- **Audit** — `Reads, writes, updates, and deletes are logged. Private versions are
  scrubbed from audit and conflict history.`
- **Portable** — `A local SQLite file. No external database daemon. Back it up like a
  file.`

Note under the section: `Illustrative states. The package enforces the real behavior.`
Do not label the privacy example as a real credential; use a fictional placeholder only.

## 4.8 05 / Under the hood — "A small local engine, not a cloud dependency."

Kicker: `05 / UNDER THE HOOD`
H2: `A small local engine, not a cloud dependency.`
Lede:
> Mesh stores memory in SQLite WAL, embeds locally, and fuses five retrieval signals. It
> starts in milliseconds, works offline, and asks before it does anything in the
> background.

Stat row (5 stats, wrap to 2 columns on mobile):
- `<0.1 ms` / `dense cosine`
- `384d` / `embeddings`
- `5` / `retrieval stages`
- `10` / `MCP tools`
- `0` / `external database daemons`

Architecture diagram (4 groups, left→right on desktop, stacked on mobile):
1. **Agents** — `Claude Code`, `Cursor`, `Codex CLI`, `custom agent`.
2. **Interfaces** — `MCP`, `HTTP`, `CLI`, `Python SDK`.
3. **Core** — `namespaces`, `consent`, `auth`, `sanitize`, `dedupe`, `contradictions`,
   `TTL`.
4. **Storage** — `SQLite WAL`, `FTS5`, `vector cache`, `ONNX / hash-384`.
Use bordered panels with mono labels and small chips; 1px connector lines with a tiny
lime dot pulsing along them when in view. The core panel can be visually dominant
(lime-tinted top border or double border). Add a footer line:
`local only · no external daemon · mode 0700`.

Spec table (2 columns, hairline rows):
| Label | Value |
| --- | --- |
| Storage | SQLite WAL per namespace, `~/.mesh/`, mode `0700` |
| Embeddings | `all-MiniLM-L6-v2` ONNX, 384d, optional ~90 MB download |
| Fallback | `hash-384` feature-hash engine, `<0.05 ms`, auto-heals when ONNX arrives |
| Retrieval | dense + BM25 + IDF + synonyms + exact tokens |
| Interfaces | MCP (10 tools), HTTP REST, CLI, Python SDK, dashboard |
| Runtime | Python 3.10+, MIT, v0.6.3 |
| Network | Local by default; cloud log sync and update checks are opt-in |

Doctor card (example output, label `example output`):
```
$ mesh doctor
ok  sqlite fts5
ok  permissions 0700
ok  onnx sha256
ok  daemon + mcp config
```
Use a compact terminal panel to the side of the spec table; on mobile, below it.

Footnote: `Performance figures are the documented dense-cosine and hash-384 fallback
figures, not an end-to-end benchmark.`

## 4.9 06 / Interfaces — "Plug it into the agents you already run."

Kicker: `06 / INTERFACES`
H2: `Plug it into the agents you already run.`
Lede:
> Mesh speaks MCP, HTTP, the CLI, and Python. Start with one interface; the same
> namespace is available to every other.

Agent compatibility strip:
- A grid/wrap of text chips: `Claude Code`, `Cursor`, `Codex CLI`, `Muse`, `Dots`,
  `OpenClaw`, `Manus`, `Grok`, `Grok Bot`, `+18 via Agensi`.
- Line: `Via Agensi, Mesh is listed for 27 agents. The same package works with any MCP
  client, or directly through Python, the CLI, and the local HTTP API.`
- No logos. Text only. The `+18 via Agensi` chip links to the listing.

Interface tabs: `Python SDK`, `MCP`, `CLI`, `HTTP REST`. Use a bordered tab strip and a
code block with a copy button. Show the exact snippets below (you may trim comments, but
not change commands or field names):

**Python SDK**
```python
from mesh import Mesh

# Agent 1 learns an architecture constraint.
agent_a = Mesh(namespace="checkout-service", agent_id="backend-agent")
agent_a.learn(
    "Production PostgreSQL 16 database runs on port 5432 with pgBouncer connection pooling",
    memory_type="fact",
    tags=["db", "postgres", "prod"],
)

# Agent 2 recalls it without talking to Agent 1.
agent_b = Mesh(namespace="checkout-service", agent_id="frontend-agent")
results = agent_b.recall("what port does production postgres use?", n=3)
```

**MCP**
```json
{
  "mcpServers": {
    "mesh": {
      "command": "mesh-server",
      "env": {
        "MESH_NAMESPACE": "my-project",
        "MESH_AGENT_ID": "claude-desktop"
      }
    }
  }
}
```
Add a mono note: `Works with Claude Desktop, Cursor, Windsurf, and other MCP clients.
Mesh ships 10 tools and a bundled agent skill (mesh install-skills).`

**CLI**
```bash
mesh add "PostgreSQL 16 runs on port 5432" --tags db,prod --type fact
mesh ask "what port does postgres use?" --n 3
mesh conflicts
mesh context --query "database config" --format markdown
```

**HTTP REST**
```bash
mesh-http --port 7701

TOKEN=$(cat ~/.mesh/auth_token)

curl -s -X POST http://127.0.0.1:7701/recall \
  -H "Content-Type: application/json" \
  -H "X-Mesh-Token: $TOKEN" \
  -d '{"query": "auth JWT algorithm", "n": 3}'
```
Mono note: `Local only. Binding to a non-loopback interface requires MESH_ALLOWED_HOSTS.`

Each code block has a copy button with a toast. Do not fetch or execute any of these
snippets.

## 4.10 07 / Distribution — Agensi

Kicker: `07 / DISTRIBUTION`
H2: `Free on Agensi. Security scanned. 27 agents.`
Body:
> Mesh is published as a free, security-scanned SKILL.md skill on Agensi. Use it from
> Claude Code, Cursor, Codex CLI, Muse, Dots, OpenClaw, Manus, Grok, and 18 more — or
> install the package yourself.

Badges: `Free`, `Security scanned`, `27 agents`.
Primary CTA: **Use this skill on Agensi ↗** → `https://www.agensi.io/skills/mesh-context-layer`
Secondary: **Read the listing ↗** → same URL.
External link behavior: `target="_blank" rel="noopener noreferrer"`, external-link icon,
visually hidden `(opens in a new tab)`.

Right visual: an ink terminal card showing the Agensi example session:
```
$ memory: "staging db moved to :5439"
ok  updated db-cfg-92
ok  resolved conf-412
ok  all agents recall :5439
```
Label: `example session from the Agensi listing`.
Below: two small mono rows:
`db-cfg-92 / staging database port is 5439 / fact`
`conf-412 / overwrote old port (5432) / conflict`

Visual treatment: a panel with `background: var(--surface-2)`, 1px ink border, 4px hard
shadow, and a 4px lime top bar. This is one of the few places a tinted panel is allowed.
Do not use the Agensi logo or product-media image. The word "Agensi" is text.

Footnote: `Free and security-scanned are Agensi listing attributes. Agent compatibility is
as listed on Agensi.`

## 4.11 08 / Get started — "Three commands to a shared memory."

Kicker: `08 / GET STARTED`
H2: `Three commands to a shared memory.`
Lede:
> Install the package, run setup, teach it one thing. Mesh does the rest locally.

Three numbered steps in a large terminal card (or three stacked terminal rows):
1. **Install the package** — `pip install mesh-context-layer`
2. **Run setup** — `mesh setup` — note: `Asks y/n before starting the daemon, registering
   auto-start, or downloading the ~90 MB model.`
3. **Teach it something** —
   `mesh add "PostgreSQL 16 runs on port 5432" --tags db,prod --type fact`
   `mesh ask "what port does postgres use?" --n 3`

Each command has a copy button. The step labels are mono; the commands are JetBrains Mono
12.5px; the note is Instrument Sans 14px muted.

CTA row: **Copy install command** (primary; copies and toasts), **View on GitHub ↗**,
**Use on Agensi ↗**.

Meta: `Python 3.10+ · MIT · v0.6.3 · local-first`.

"What gets created" mini-panel:
- `~/.mesh/` — mode `0700`
- `~/.mesh/mesh_<namespace>.db` — SQLite WAL
- `~/.mesh/models/` — optional ONNX weights
- `~/.mesh/auth_token` — mode `0600`
Footnote: `No silent background daemons or downloads.`

## 4.12 09 / FAQ — "Questions, answered."

Kicker: `09 / FAQ`
H2: `Questions, answered.`
Accordion, one open at a time. Answers exactly:

1. **Is Mesh a vector database?**
   > No. Mesh is a memory layer. It uses SQLite WAL and FTS5 for storage and retrieval,
   > with a local vector cache and local embeddings. There is no external database daemon
   > to run.

2. **Does my data leave my machine?**
   > Not by default. Mesh is local-first and does not silently phone home. Cloud log sync
   > and update checks are explicit opt-ins.

3. **What happens when two agents learn conflicting facts?**
   > Mesh flags the contradiction for review instead of silently overwriting. You can
   > inspect and resolve it from the CLI, MCP, or the dashboard.

4. **Do I need a GPU or an API key?**
   > No GPU and no API key for local use. Mesh runs CPU embeddings via ONNX; if the model
   > is unavailable, it falls back to the built-in hash-384 engine.

5. **Which agents does it work with?**
   > Any MCP-compatible client, plus the Python SDK, CLI, and local HTTP API. Via Agensi,
   > it is listed for 27 agents, including Claude Code, Cursor, Codex CLI, Muse, Dots,
   > OpenClaw, Manus, Grok, and Grok Bot.

6. **How fast is recall?**
   > The documented dense vector cosine stage runs in under 0.1 ms on the local matrix
   > cache; the hash-384 fallback serves requests in under 0.05 ms. End-to-end recall
   > depends on corpus size and the stages involved.

7. **Is it production-ready?**
   > Mesh is MIT-licensed and at v0.6.3. It is a local-first library and local server,
   > not a hosted service. Read the security and consent notes in the README before
   > deploying it in a shared environment.

End with a quiet link: `Still curious? Read the README on GitHub ↗`.

## 4.13 Final CTA + footer

Final CTA (centered, generous whitespace, optional faint grid):
Kicker: `FINAL`
H2: `One agent learns. Every agent knows.`
Lede: `Install Mesh, teach it once, and stop paying the amnesia tax.`
CTAs: **Install mesh** (copy) + **Use on Agensi ↗** (external).
Terminal line: `$ mesh ask "what did we already learn?"` with a blinking lime cursor.

Footer:
- Top row: brand lockup (mark + "mesh"), one-line description `Shared semantic memory for
  AI agents.`
- Columns:
  - **Product**: How it works, Search, Memory, Install
  - **Interfaces**: MCP, HTTP REST, CLI, Python SDK
  - **Resources**: GitHub, PyPI, Changelog, Agensi
  - **Legal**: MIT License
- Bottom row: `© 2026 Mesh · MIT · local-first` + `Back to top ↑` + a small mono status
  `local engine / no external daemon`.
- A huge outlined `mesh` wordmark at the bottom:
  - Martian Mono 600, `font-size: clamp(6rem, 18vw, 16rem)`, uppercase? No, lowercase
    `mesh`.
  - `color: transparent; -webkit-text-stroke: 1px var(--line);`
  - `line-height: .8; overflow: clip;` and a lime block cursor after it.
  - Do not let it create horizontal scroll. Scale it to the container; `overflow: clip`.
- Back-to-top link scrolls to `#top`; respect reduced motion.

---

# 5. Interaction implementation details

## 5.1 State machines

**How-it-works:** `step ∈ {0,1,2,3}` where 0 idle, 1 learned, 2 indexed, 3 recalled.
- `run`: clear timers; step 0; after 80ms step 1; after 900ms step 2; after 1800ms step 3.
- `learn`: clear timers; step 1.
- `recall`: clear timers; step 3.
- `reset`: clear timers; step 0.
- Auto-play once on first intersection if motion on; set `autoPlayed.current = true`.
- Clean up timers in `useEffect(() => () => clearTimers(), [])`.

**Search explorer:** `query`, `sampleId`, `mode`, `running`, `revealed` (0–5).
- `run`: validate; set running; reveal 0; interval 110ms → revealed++; at 5 clear, set
  running false. If motion off, set revealed 5 immediately.
- Changing mode resets revealed to 0 and shows the result card dimmed until run? Simpler:
  changing mode re-runs instantly for the active sample (no stagger if motion off). Keep
  the last result visible but update the fusion summary. Do not leave a blank panel.
- Clicking a sample sets query and runs.
- Empty query: inline message, no run.

**Memory tabs:** each tab owns its demo state; do not reset other tabs when switching.
- Dedupe: `merged` boolean; `Learn it again` sets true; `Reset` sets false.
- Contradictions: `resolved` boolean; `Resolve` sets true; `Reset` sets false.
- TTL: `days` 0/30/60/90; confidence map; reset to 0. Buttons disabled at 90 or wrap?
  Disable at 90 with label `stale`.
- Privacy: `includePrivate` boolean; default false.

**Theme:** `pref ∈ {system,light,dark}`; resolved from `matchMedia` when system. Persist,
update `<html data-theme>`, `data-theme-pref`, `style.colorScheme`. Listen for OS changes.
**Motion:** `on/off`; persist, update `<html data-motion>`. Initialize from
`prefers-reduced-motion` if no stored preference. When off, all demo animations snap.

## 5.2 Copy behavior

Use a `useCopy` hook:
```js
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) {}
  // Fallback: hidden textarea + execCommand("copy"), then remove it.
  return false;
}
```
If it fails, select the text and show a toast `Press Ctrl/Cmd+C to copy`; never use
`alert`/`confirm`/`prompt`. Every copy action announces a toast via `role="status"`.

## 5.3 Scroll and observers

- Smooth scrolling only when motion is on and the OS allows it. Use `scroll-behavior:
  smooth` on `<html>`; set `.section { scroll-margin-top: calc(var(--topbar-h) + 24px); }`.
- Reveal observer: `{ threshold: 0.15, rootMargin: "0px 0px -10% 0px" }`, unobserve after
  entering. If `data-motion="off"`, skip the observer and mark visible.
- Auto-play observer for the How demo: threshold 0.4, once.
- Scroll progress: passive listener + rAF; compute
  `scrollY / (documentElement.scrollHeight - innerHeight)`, clamp 0–1.
- Do not use scroll-jacking, parallax on text, or pin sections.

## 5.4 Icons

Draw all icons inline as SVG: arrow-up-right, arrow-right, copy, check, play, replay,
menu, close, sun, moon, monitor, motion, shield, database, spark, lock. Use a 16/20/24
viewBox, `fill="none"`, `stroke="currentColor"`, `stroke-width="1.5"`,
`stroke-linecap="round"`, `stroke-linejoin="round"`. Do not install an icon library. Mark
decorative icons `aria-hidden="true"`; icon-only buttons need `aria-label`.

---

# 6. Responsive behavior

Breakpoints: 480, 768, 900, 1024, 1280.

- **≥1024:** hero 2 columns (copy 1.05fr / visual .95fr), demo panels side by side,
  section headers 2 columns, footer 4 columns.
- **768–1023:** hero stacks with copy first; demos stack; nav may still fit; reduce
  display sizes.
- **<900:** topbar collapses to menu; section headers stack; tabs scroll horizontally.
- **<768:** all grids single column; stat row 2 columns; footer 2 columns; the hero
  terminal is full width; code blocks scroll internally.
- **360–479:** page gutter 20px; h1 min 2.25rem; buttons full-width in CTA rows (or wrap
  cleanly); no horizontal page scroll. Test the terminal, code blocks, marquee, footer
  wordmark, and architecture diagram specifically.
- Touch targets ≥44×44px. Hover-only effects must be wrapped in
  `@media (hover: hover) and (pointer: fine)`. Do not use cursor-following effects on
  coarse pointers.
- The topbar must not overlap content: `scroll-margin-top` on all anchor targets.

---

# 7. Accessibility requirements

- One `<h1>` (hero). Logical h2/h3 order. Sections use `aria-labelledby`.
- Skip link: `Skip to content` → `#main`.
- Landmarks: `<header>`, `<nav>`, `<main id="main">`, `<footer>`.
- Focus-visible: 2px `--focus` outline, 2px offset. Never `outline: none` without a
  replacement.
- All controls are native `<button>`, `<a>`, `<input>`, or elements with complete ARIA
  patterns. No clickable `<div>`.
- Tabs: arrow keys, Home/End, `aria-selected`, `tabIndex` roving. Accordion:
  `aria-expanded`, `aria-controls`, Enter/Space. Switch: `role="switch"`, `aria-checked`.
  Theme radiogroup: arrow keys.
- Live regions: demo note and toast use `role="status"`/`aria-live="polite"`; don't
  announce every animation tick. Announce stage completion once.
- Decorative SVG/diagrams: `aria-hidden="true"`; diagrams that convey information get
  `role="img"` + `aria-label` + a text summary nearby.
- Marquee duplicates: `aria-hidden="true"`; the accessible copy lists names once.
- External links: `rel="noopener noreferrer"`, plus a visually hidden "(opens in a new
  tab)".
- Contrast: verify `--muted` on `--bg`, `--accent-text` on `--bg`, `--ink` on `--accent`,
  and terminal text. Do not use lime text on white except `--accent-text`.
- No flashing more than 3 times per second. No autoplay audio/video. No color-only state:
  pair badges/labels with text/icons.
- `prefers-reduced-motion`: full static experience; demos still work via buttons and show
  final states.
- Keyboard-test the menu, theme/motion controls, hero CTAs, all demo controls, tabs,
  accordion, copy buttons, and footer links.
- The mobile menu traps focus and closes on Escape; restore focus to the trigger.

---

# 8. SEO, metadata, PWA

`index.html` must include:

- `<html lang="en">`, `<meta charset="utf-8">`,
  `<meta name="viewport" content="width=device-width, initial-scale=1">`.
- `<title>Mesh — shared memory for AI agents</title>`
- Description: `Mesh is a high-speed shared semantic and hybrid memory layer for AI
  agents. One agent learns a fact; every other agent recalls it locally in under 0.1 ms.`
- Canonical `SITE_URL` (define `SITE_URL = "https://mesh.example"` in
  `src/data/content.js`; this is the only allowed TODO — replace it with the production
  origin at deploy time).
- Open Graph: `og:type=website`, `og:title`, `og:description`, `og:url=SITE_URL`,
  `og:image=${SITE_URL}/assets/brand/og-image.png`, `og:image:width=1200`,
  `og:image:height=630`, `og:site_name=Mesh`.
- Twitter: `summary_large_image`, title, description, image.
- Theme color: two tags with media queries — light `#F7F8F3`, dark `#0B0D08`.
- Favicon/icon/manifest/apple/mask-icon links exactly as in
  `landing/assets/brand/README.md`.
- `<meta name="color-scheme" content="light dark">`.
- JSON-LD `SoftwareApplication`:
  - name Mesh, applicationCategory DeveloperApplication, operatingSystem macOS/Linux/
    Windows, offers price 0 USD, license MIT, url GitHub, sameAs PyPI + Agensi.
  - Do not include aggregateRating.
- `robots` index/follow.

The site uses `public/assets/brand/` (copy the folder from `landing/assets/brand/`).
The manifest already uses absolute `/assets/brand/...` paths. If the site is deployed
under a subpath, update the manifest `start_url`, `scope`, and icon paths, and the
`SITE_URL`; otherwise leave them.

No service worker is required. Do not add analytics, cookie banners, chat widgets, or
newsletter forms.

---

# 9. Performance

- Target: < 500 KB JS+CSS gzipped including React; < 1.2 MB total excluding fonts.
- Use the SVG mark/lockup above the fold, not a large PNG. The OG image is not loaded in
  the page.
- Set `width`/`height` on every `<img>` and aspect-ratio on terminal/diagram panels to
  prevent CLS.
- Google Fonts with `display=swap`; preconnect. Do not load unused weights.
- Animate only `transform` and `opacity`; avoid animating width/height/top/left/box-shadow
  on scroll. The progress bar width is fine (single element).
- Use one IntersectionObserver per concern, not one per element where practical. Clean up
  all observers/listeners/timers on unmount.
- Throttle scroll with rAF; passive listeners.
- Avoid layout reads in loops; cache `getBoundingClientRect` per pointer move if needed.
- No heavy libraries. No Lottie, Three.js, Remotion, GSAP, Framer Motion, or video.
- Lighthouse targets (desktop): Performance ≥ 90, Accessibility ≥ 95, Best Practices ≥ 95,
  SEO ≥ 95. If fonts hurt performance, keep the targets honest and note it.
- Test with network throttling: the page must be usable before the terminal auto-play
  finishes; never block content on JS.

---

# 10. File structure

Create the app in `landing/site/`. Suggested structure; keep files small and focused. Do
not put the whole page in one 3,000-line `App.jsx`.

```
landing/site/
  index.html
  package.json
  vite.config.js
  public/
    assets/brand/            # copy of landing/assets/brand/
  src/
    main.jsx
    App.jsx
    styles/
      tokens.css
      base.css
      layout.css
      components.css
      sections.css
      motion.css
    data/
      content.js             # SITE_URL, nav, copy deck, samples, FAQ, interfaces
    hooks/
      useTheme.js
      useMotion.js
      useReveal.js
      useCopy.js
      useScrollProgress.js
      useReducedMotion.js
    components/
      Topbar.jsx
      Hero.jsx
      Terminal.jsx
      AgentFlow.jsx
      TrustStrip.jsx
      SectionHeader.jsx
      ProblemSection.jsx
      HowItWorks.jsx
      SearchExplorer.jsx
      MemorySection.jsx
      UnderTheHood.jsx
      InterfacesSection.jsx
      AgensiSection.jsx
      InstallSection.jsx
      FaqSection.jsx
      FinalCta.jsx
      Footer.jsx
      ui/
        Button.jsx
        Badge.jsx
        Panel.jsx
        Tabs.jsx
        Switch.jsx
        Segmented.jsx
        CopyButton.jsx
        Reveal.jsx
        Marquee.jsx
        Accordion.jsx
        Toast.jsx
        Icon.jsx
```

Copy the brand folder exactly:

```powershell
# from the repo root
New-Item -ItemType Directory -Force landing/site/public/assets | Out-Null
Copy-Item -Recurse -Force landing/assets/brand landing/site/public/assets/brand
```

Do not edit the copied assets. If a path needs changing, change the manifest in the
source first and re-copy it; do not edit one copy only.

`package.json` (use these versions or newer compatible patch releases):

```json
{
  "name": "mesh-landing",
  "private": true,
  "version": "1.0.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "18.3.1",
    "react-dom": "18.3.1"
  },
  "devDependencies": {
    "@vitejs/plugin-react": "^4.3.4",
    "vite": "^6.0.0"
  }
}
```

`vite.config.js`:

```js
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
});
```

No router. No state library. No CSS framework. No ESLint setup is required, but the build
must be clean.

---

# 11. Build order

Work in this order; do not jump straight into fancy demos.

1. Scaffold Vite + React; copy the brand assets; add the font links; add tokens and base
   CSS.
2. Build the topbar, hero, terminal, and trust strip. Verify light/dark and 360/1440
   before moving on.
3. Build section headers and the problem section with the without/with toggle.
4. Build How-it-works and its state machine. Verify timers and reduced motion.
5. Build the search explorer and its sample data. Verify keyboard and empty-query state.
6. Build the memory tabs and the four small demos.
7. Build under-the-hood, interfaces, Agensi, install, FAQ, final CTA, footer.
8. Add theme/motion persistence, reveal observers, scroll progress, marquees, counters.
9. Run responsive and accessibility passes; fix.
10. Build, preview, screenshot, and write the handoff report.

After each phase, run `npm run build` and fix errors immediately. Do not accumulate a giant
broken branch.

---

# 12. QA checklist

Run through every item. Report failures you could not fix.

**Build and runtime**
- [ ] `npm install` succeeds with no peer dependency errors.
- [ ] `npm run dev` serves the page at `http://localhost:5173`.
- [ ] `npm run build` completes with no errors; `npm run preview` serves the build.
- [ ] No console errors; no 404s for fonts, brand assets, manifest, favicon.
- [ ] No unused imports or dead demo code.

**Design**
- [ ] Kernel only: white-first, acid lime, hard offset shadows, 1–2px radii, no glass,
      no pill cards, no purple/blue gradients.
- [ ] Martian Mono is visibly the display face; body is Instrument Sans; code is JetBrains
      Mono. No fourth family.
- [ ] The approved three-node mark is used everywhere. No Agensi logo, no third-party
      logos.
- [ ] The page has a clear story rhythm and no section feels like filler.
- [ ] Light mode is the strongest; dark mode is a designed palette, not an inversion.

**Theme and motion**
- [ ] No light/dark flash on load.
- [ ] System/Light/Dark all work and persist across reloads.
- [ ] OS theme changes update the page live while preference is System.
- [ ] Motion toggle persists; `prefers-reduced-motion` starts motion off.
- [ ] Motion off: no auto-play, marquee, typing, packets, counters, or spotlight; demos
      still work and show final states.
- [ ] No animation runs longer than 2s; no flashing; no scroll-jacking.

**Interactions**
- [ ] Hero install copies the exact command and toasts.
- [ ] How-it-works: Run, Learn, Recall, Reset all work; auto-play once; timers clean up.
- [ ] Search: three samples, Enter key, empty-query message, hybrid/vector toggle, result
      updates, no network.
- [ ] Memory: dedupe merge, contradiction resolve, TTL advance/reset, privacy toggle.
- [ ] Interfaces tabs: Python, MCP, CLI, HTTP; copy buttons work.
- [ ] FAQ accordion: one open, keyboard accessible, correct answers.
- [ ] Mobile menu: opens, traps focus, Escape closes, links work, focus restored.
- [ ] Every external link opens safely and points to the correct URL.

**Responsive**
- [ ] 1440, 1280, 1024, 900, 768, 390, 360 widths: no horizontal page scroll.
- [ ] Hero, terminal, demos, architecture diagram, stats, footer wordmark all adapt.
- [ ] Code blocks scroll internally, not the page.
- [ ] Touch targets ≥44px; no hover-only controls on touch.
- [ ] No clipped text; no overlapping elements.

**Accessibility**
- [ ] Keyboard-only pass through the whole page.
- [ ] Focus is always visible.
- [ ] One h1; logical headings; landmarks; skip link.
- [ ] Tabs/switch/accordion/menu have correct ARIA and keyboard behavior.
- [ ] Live regions announce step completion and copy results, not every tick.
- [ ] Contrast passes for body, muted, accent text, buttons, terminal.
- [ ] Decorative SVGs hidden; informative diagrams labeled.
- [ ] Reduced motion is honored by all demos.

**SEO/PWA**
- [ ] Title, description, canonical, OG/Twitter tags, JSON-LD.
- [ ] Favicon (ICO + SVG), apple-touch, mask-icon, manifest, browserconfig.
- [ ] Manifest icons resolve; theme/background colors match tokens.
- [ ] `SITE_URL` is the only placeholder left, and it is documented in the handoff.

**Content**
- [ ] No invented claims, testimonials, logos, ratings, or benchmarks.
- [ ] Every performance number matches the approved list.
- [ ] Demos are labeled example/simulated.
- [ ] Agensi claims are attributed to the listing.
- [ ] No "lorem ipsum", no placeholder copy.

---

# 13. Definition of done

The work is done when:

1. The site lives in `landing/site/` and runs with `npm run dev`.
2. `npm run build` succeeds and `npm run preview` works.
3. All 13 page regions (topbar through footer) are implemented with the copy deck.
4. All five interactive demos work, including keyboard and reduced-motion paths.
5. Light/dark/system, motion on/off, and persistence work with no flash.
6. The page is responsive from 360px to 1440px with no horizontal scroll.
7. The approved brand assets are used; no Agensi logo or third-party logos.
8. The QA checklist is complete; remaining failures are listed explicitly.
9. The handoff report states what was built, how to run/build/deploy, the `SITE_URL`
   placeholder, and any deviations from this brief.

---

# 14. Do not list

- Do not modify `landing/direction-sampler.html`, `landing/assets/brand/`, or any Python
  file.
- Do not use the Agensi product-media logo, the Agensi logo, or any agent logo.
- Do not use Atlas, Signal, or Drift tokens or layouts.
- Do not use TypeScript, Tailwind, CSS-in-JS, styled-components, Material UI, Chakra,
  shadcn, Bootstrap, or any UI/icon/animation library.
- Do not use Remotion, GSAP, Framer Motion, Lottie, Three.js, or video in the site runtime.
  Remotion is for making a promo video later, not for the landing page.
- Do not use `alert`, `confirm`, `prompt`, or `document.write`.
- Do not use a pure white or pure black page ground; use the tokens.
- Do not use gradients as primary surfaces, neon glows, glassmorphism, blurry shadows, or
  rounded-pill cards. Status pills are the only pill shape.
- Do not use lime text on a light surface; use `--accent-text`.
- Do not invent benchmarks, customer logos, testimonials, ratings, "trusted by" claims,
  "AI-powered" filler, or "revolutionary" language.
- Do not claim cloud sync, a hosted service, GPU acceleration, zero configuration, or
  automatic internet access.
- Do not add analytics, cookie banners, chat widgets, newsletter forms, or tracking pixels.
- Do not auto-play audio/video, hijack scrolling, or add parallax to text.
- Do not put a giant image above the fold; use the SVG mark/terminal.
- Do not leave placeholder copy or broken links. The only allowed placeholder is the
  documented `SITE_URL`.
- Do not commit secrets, tokens, or machine-specific absolute paths.
- Do not put the sampler or this brief in `public/`.

---

# 15. Handoff report

When finished, write a short report at the end of your response (or in
`landing/site/HANDOFF.md`) with:

- What was built, section by section.
- How to run: `cd landing/site && npm install && npm run dev`.
- How to build: `npm run build`; deploy `dist/`.
- The `SITE_URL` placeholder and where to change it.
- Any deviations from this brief and why.
- Any QA items that failed or could not be tested.
- Screenshots or a description of the light/dark, 1440/390 result if you can run a
  browser.

Do not claim a check passed if you did not run it. Be precise.
