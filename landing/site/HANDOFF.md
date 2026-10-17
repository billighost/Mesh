# Mesh landing site — handoff

A single-page, story-driven landing site for **Mesh** (`mesh-context-layer`), built from
`landing/BUILD_PROMPT.md` and the approved **Kernel** direction in
`landing/direction-sampler.html`.

## Run it

```bash
cd landing/site
npm install
npm run dev       # http://localhost:5173
npm run build     # production build → dist/
npm run preview   # serve the production build → http://localhost:4173
```

Requires Node 18+.

## Deploy

`dist/` is a fully static site. Upload it to any static host (Vercel, Netlify, Cloudflare
Pages, GitHub Pages, S3 + CDN). No server-side runtime is needed.

Before going live:

1. Replace the placeholder `SITE_URL` in `src/data/content.js` with the production URL.
2. Replace the canonical URL and OG image URLs in `index.html` (`https://mesh.example/`).
3. Point the GitHub / PyPI / Agensi links in `src/data/content.js` at the real destinations.
4. If the site is served from a subpath, set `base` in `vite.config.js` (e.g. `"/mesh/"`).

## What is in here

```
landing/site/
  index.html                 document shell, SEO meta, JSON-LD, inline theme boot
  public/assets/brand/       the 30 approved brand assets (SVG, PNG, ICO, manifest)
  src/
    App.jsx                  page composition
    data/content.js          all copy, nav, demo data, links
    components/              one component per page section + UI primitives
    hooks/                   preferences, in-view, clipboard
    lib/highlight.js         highlight.js core + python / json / bash only
    styles/                  tokens, base, components, sections, motion
  dist/                      production output (git-ignored)
```

## Design and behavior

- **Kernel direction**: white-first palette, acid-lime accent `#B9F24A`, ink `#0B0D08`,
  hard offset shadows, tight radii, Martian Mono display with Instrument Sans body.
- **Theme**: light is the default on a first visit. Light / System / Dark are selectable in
  the top bar and the mobile menu; the choice persists in `localStorage` under `mesh-theme`
  and is applied before first paint by the inline boot script.
- **Motion**: a Motion switch (persisted under `mesh-motion`) soft-disables animation.
  `prefers-reduced-motion` is also honored.
- **Content**: edit `src/data/content.js`; all section copy, demo facts, scores, links and
  navigation live there.
- **Demos**: How It Works, Search Explorer, and Memory (dedupe / contradictions / TTL /
  privacy) are deterministic and illustrative. Search and memory demo rows are labeled
  "example data" in the UI.

## Motion system

The preview's motion vocabulary is used throughout:

- magnetic pull on every `Button` (`components/ui/Magnetic.jsx`)
- spring press/bounce on buttons (`btnBounce` in `components.css`)
- cursor spotlight (`components/ui/CursorSpotlight.jsx`)
- 3D tilt on the fused result card (`components/ui/Tilt.jsx`)
- text scramble on the final headline (`components/ui/Scramble.jsx`)
- five-stage boot loader (`components/BootLoader.jsx`)
- staggered mobile menu entrance/exit (`menuIn` / `menuOut` in `sections.css`)
- scroll reveals, marquee, terminal line stagger, how-it-works packets, TTL/merge
  animations, and search-stage fills

## Code highlighting

`highlight.js` core is used with only three grammars (Python, JSON, Bash) registered, so
the full language pack is never shipped. Token colors are theme-independent inside the
dark terminal panels and defined in `components.css` under `hljs-*`.

## Accessibility

- Skip link, semantic landmarks, one `h1`, labeled sections.
- Keyboard support: tabs (arrows/Home/End), segmented controls (arrows), accordion,
  mobile menu focus trap + Escape + scroll lock, `aria-live` status lines.
- Motion off removes non-essential animation; theme and motion controls are real
  `radiogroup` / `switch` widgets.
- Demo copy is illustrative, not presented as a live benchmark.

## QA performed

- Production build passes (`vite build`).
- Desktop at 1440×900 and mobile at 390×844 / 320×720: no horizontal page scroll.
- Light and dark palettes checked on hero, search explorer, and interfaces.
- Theme default, boot loader, magnetic transform, press bounce, spotlight variables,
  menu open/close animation, tilt and scramble verified in the running build.
- No console errors in the production preview.

Final bundle: ~76.7 kB gzip JS, ~11.2 kB gzip CSS, ~1.6 kB gzip HTML.

## Do not change

- Do not use the Agensi logo. The mark in `public/assets/brand/` and the preview is the
  approved logo.
- `landing/direction-sampler.html` and `landing/assets/brand/` are source references; the
  site consumes copies under `public/assets/brand/`.
