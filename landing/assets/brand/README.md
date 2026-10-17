# Mesh brand assets — Kernel

This folder is the approved brand asset set for the Mesh landing page. The mark is
the three-node glyph from `../direction-sampler.html` (the Kernel direction), not the
Agensi listing logo. The wordmark is **Martian Mono SemiExpanded Bold**, converted to
outlines so the lockups do not depend on a webfont being loaded.

## Mark

- Three nodes, two upper diagonals, one lower baseline. The lower baseline is a link,
  not a container. Do not close the triangle or add a fourth node.
- Source geometry lives in a `64 × 64` viewBox: nodes at `(32,13)`, `(12,50)`, `(52,50)`,
  radius `7`, stroke `4.5`, round caps.
- The app icon is an ink (`#0B0D08`) rounded square (`rx 14/64`) with a lime
  (`#B9F24A`) mark. The maskable variant is full-bleed ink with the mark scaled to
  `0.72` so it stays inside the safe zone.

## Color

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| Ground | `#F7F8F3` | `#0B0D08` | Page background |
| Surface | `#FFFFFF` | `#11140C` | Cards, panels |
| Surface 2 | `#EEF0E7` | `#171B11` | Inset panels, code chips |
| Ink | `#0B0D08` | `#F4F7EC` | Text, borders, shadows |
| Muted | `#5D6455` | `#A2AA96` | Secondary text |
| Line | `#D7DBCD` | `#2A2F22` | Hairlines, dividers |
| Accent | `#B9F24A` | `#C8F04A` | Signal fills, highlights |
| Accent text | `#2F6B00` | `#C8F04A` | Accent-colored *text* |
| Accent soft | `#E8F9C7` | `#1E2A0E` | Tinted backgrounds |

The accent is a signal color. Do not use `#B9F24A` as body text on white — use the
accent-text token instead. The ink-on-lime pairing is the primary button treatment.

## Assets

### SVG (preferred for web)

| File | Use |
| --- | --- |
| `mesh-mark.svg` | Ink mark on light surfaces |
| `mesh-mark-light.svg` | Off-white mark on ink surfaces |
| `mesh-mark-accent.svg` | Lime mark on ink surfaces, or as an accent |
| `mesh-mark-currentColor.svg` | Inline mark that inherits `currentColor` |
| `mesh-logo.svg` | Ink mark + ink wordmark on light surfaces |
| `mesh-logo-light.svg` | Lime mark + off-white wordmark on ink surfaces |
| `mesh-app-icon.svg` | Rounded app icon / favicon / PWA icon |
| `mesh-app-icon-maskable.svg` | Full-bleed maskable icon |
| `favicon.svg` | SVG favicon (same artwork as the app icon) |
| `safari-pinned-tab.svg` | Monochrome Safari pinned-tab mask |

### PNG

| File | Size | Use |
| --- | --- | --- |
| `favicon-16.png` | 16 | Legacy favicon |
| `favicon-32.png` | 32 | Legacy favicon |
| `favicon-48.png` | 48 | Legacy favicon |
| `apple-touch-icon-180.png` | 180 | iOS home screen |
| `icon-192.png` | 192 | PWA any |
| `icon-512.png` | 512 | PWA any / social avatar |
| `icon-1024.png` | 1024 | App stores, press |
| `icon-maskable-192.png` | 192 | PWA maskable |
| `icon-maskable-512.png` | 512 | PWA maskable |
| `mstile-150x150.png` | 150 | Windows tile |
| `mesh-mark-512.png` | 512 | Transparent ink mark |
| `mesh-mark-light-512.png` | 512 | Transparent off-white mark |
| `mesh-mark-accent-512.png` | 512 | Transparent lime mark |
| `mesh-logo.png` | 1200×320 | Transparent ink lockup |
| `mesh-logo-light.png` | 1200×320 | Transparent light lockup; **use on dark backgrounds only** |
| `og-image.png` | 1200×630 | Open Graph / X / LinkedIn preview |
| `favicon.ico` | 16/32/48 | Browser tab fallback |

### Metadata

| File | Use |
| --- | --- |
| `site.webmanifest` | PWA manifest; assumes the folder is served at `/assets/brand/` |
| `browserconfig.xml` | Windows tile config |

## HTML head

These paths assume the folder is copied/served at **`/assets/brand/`**. If the site
lives under a subpath, adjust the absolute paths (and `start_url` / `scope` in the
manifest).

```html
<link rel="icon" href="/assets/brand/favicon.ico" sizes="any">
<link rel="icon" type="image/svg+xml" href="/assets/brand/favicon.svg">
<link rel="apple-touch-icon" href="/assets/brand/apple-touch-icon-180.png">
<link rel="mask-icon" href="/assets/brand/safari-pinned-tab.svg" color="#0B0D08">
<link rel="manifest" href="/assets/brand/site.webmanifest">
<meta name="msapplication-config" content="/assets/brand/browserconfig.xml">
<meta name="msapplication-TileColor" content="#0B0D08">
<meta name="theme-color" content="#0B0D08">

<meta property="og:image" content="https://mesh.example/assets/brand/og-image.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="https://mesh.example/assets/brand/og-image.png">
```

Replace `mesh.example` with the production origin. The OG image already contains the
headline, install command, and the Agensi availability line; it is a finished asset,
not a template.

## Using the mark inline

Prefer `<img src="/assets/brand/mesh-mark.svg" alt="" width="16" height="16">` next to
text, so the SVG stays cacheable and has no duplicate IDs. If you need the mark to
inherit the surrounding text color, inline `mesh-mark-currentColor.svg` and give its
`<title>` a unique `id` (the file uses `id="t"`; duplicate IDs across many inline
copies are invalid).

The mark is decorative when it sits next to the word "mesh" or the text "Mesh".
Give it `alt=""` / `aria-hidden="true"` in that case. When it is the only brand
element in a link, label the link "Mesh home".

## Usage rules

- **Clear space:** at least the outer diameter of one node (≈ `18/64` of the mark's
  width) on every side. For the lockup, keep at least `16px` clear space per `64px`
  of lockup height.
- **Minimum sizes:** mark `16px` digital / `8mm` print; horizontal lockup `96px` wide
  digital / `30mm` print.
- **Dark surfaces:** use the `-light` variants. The light lockup's wordmark is off-white
  and is intentionally invisible on white; that is not a bug.
- **Do not:** rotate, stretch, outline, add gradients, add glows, change the node count,
  close the triangle, replace the wordmark with a different typeface, or recolor the
  mark outside the provided variants.
- **Never use the Agensi listing logo.** The Agensi listing is a distribution channel;
  it is not the Mesh mark. The user explicitly rejected that artwork.
- Do not put the mark inside another shape except the supplied app-icon and maskable
  variants.
