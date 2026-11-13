# The reel

A short film about Pinecone, drawn by code: a mocked pine terminal and the Pinecone panel play a script.
Nothing in it is recorded. It is made the way pine's reel is.

| File | What it is |
| --- | --- |
| `reel.html` | the page the film is drawn on |
| `film.js` | the machinery (a state for every moment, drawn as HTML) and, below it, the story scene by scene |
| `render.mjs` | draws the film frame by frame in a headless browser and hands the frames to ffmpeg |
| `sound.py` | makes the typing and click sounds, and puts the spoken line where it is played |
| `assets/` | the logo, the font (JetBrains Mono, under the SIL Open Font License), and what Pinecone's own tools made for the film: the two pictures, the backdrop and the spoken line |

## Rendering

It needs `ffmpeg`, Python 3 with numpy for the sound, and playwright-core with its Chromium:

```bash
npm i playwright-core && npx playwright-core install chromium
node reel/render.mjs
```

writes `reel/pinecone-reel.mp4` (3840×2160; `--scale 1` for 1920×1080). A part of it, or single frames to look at:

```bash
node reel/render.mjs --from 20 --to 30
node reel/render.mjs --at 3.2,12,40
```

`--playwright <folder>` uses a `node_modules/playwright-core` that is installed somewhere else.
What the render writes (`*.mp4`, `sfx.wav`, `sfx.json`, `frames/`) is not kept in the repository.
