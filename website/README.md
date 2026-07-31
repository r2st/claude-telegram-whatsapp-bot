# website/

The source of [telechat.fyi](https://telechat.fyi). Static files, no build step,
no dependencies — what is in this directory is what is served.

| File | Purpose |
|------|---------|
| `index.html` | The whole landing page: markup, CSS and ~40 lines of JS, inline |
| `og.svg` → `og.png` | Social card. The PNG is what ships; the SVG is its source |
| `favicon.svg` | Tab icon |
| `qrcode.svg` | QR code for `telechat.fyi`, shown in the final CTA |
| `robots.txt`, `sitemap.xml` | Crawler directives |

## Why one file

The page has no framework, no bundler and no external requests — no CDN script,
no webfont, no analytics. That is enforced by `tests/test_website.py`, not just
convention. It keeps the page fast, keeps it working when a third party goes
down, and means anyone can fix a typo without installing anything.

## Editing

Open `index.html` in a browser. That is the whole loop.

Then run the tests — they catch the failures a browser will not show you:

```bash
pytest tests/test_website.py -v
```

They check that every referenced asset exists, that in-page anchors resolve,
that the JSON-LD parses and agrees with the visible FAQ, that the sitemap only
lists pages that exist, that the copy buttons paste what they display, and that
the version and install commands match `pyproject.toml` and `npm/package.json`.

## Regenerating the social card

`og.png` is checked in because deploys are a file copy. After editing `og.svg`:

```bash
magick -background none website/og.svg png24:website/og.png
```

Then **look at the PNG**. ImageMagick's SVG renderer silently drops gradients on
text and several filter effects — the first version of the card shipped with an
invisible second headline line for exactly that reason.

## Deploying

Copy the directory to the host serving `telechat.fyi`. There is nothing to
build and no server-side anything; every file is cacheable and immutable except
`index.html`.
