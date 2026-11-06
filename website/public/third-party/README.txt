TGDrive bundled viewer notices
==============================

These files accompany browser assets built from the exact versions in
website/package-lock.json. Vite copies this directory unchanged into dist;
Docker copies that dist directory into its runtime image.

Movi Player 0.4.1
-----------------
Package: https://www.npmjs.com/package/movi-player/v/0.4.1
Source: https://github.com/MrUjjwalG/movi-player/tree/v0.4.1
Published npm git revision: eaaa3088aba65454176a180ece2a2ef2af513fa3
Source archive:
https://github.com/MrUjjwalG/movi-player/archive/eaaa3088aba65454176a180ece2a2ef2af513fa3.tar.gz
WASM build recipe:
https://github.com/MrUjjwalG/movi-player/blob/eaaa3088aba65454176a180ece2a2ef2af513fa3/docker/build-ffmpeg.sh
Build environment:
https://github.com/MrUjjwalG/movi-player/blob/eaaa3088aba65454176a180ece2a2ef2af513fa3/docker/Dockerfile

movi-player-LICENSE.txt is copied unchanged from the published npm package.
It contains the Apache-2.0 player license and bundled component notices,
including FFmpeg LGPL-2.1-or-later, dav1d BSD-2-Clause, and Signalsmith MIT.
FFmpeg-LGPL-2.1.txt supplies the full LGPL-2.1 text from upstream FFmpeg.
Preserve these notices when redistributing the emitted JS and WASM assets.

TGDrive ships its reviewed modified JavaScript in a pinned npm archive
published as a TGDrive GitHub release asset. The original license and WASM
remain unchanged. See movi-player-TGDrive-patch-NOTICE.txt,
website/vendor/manifest.json and website/scripts/patch-movi-player.js for the
changes and upstream identity. Normal development/build/test commands do not
mutate the installed package.

Source and relinking archive:
https://github.com/TechShreyash/TGDrive/releases/download/viewer-movi-player-0.4.1-tgdrive.3/movi-player-0.4.1-sources.tar.gz
SHA-256: 143fb358f1dea3de094f37ae65a882e9ade4407fd273dc7055db9410ab3c3add

The archive supplies complete FFmpeg n9.0.2 and dav1d 1.5.3 source tarballs,
pinned Movi application/build/Signalsmith sources, published WASM-target static
libraries and headers, licenses, SHA-256 manifests and rebuild/relink steps.

The original notice labels FFmpeg n8.1.2; the published Dockerfile selects
n9.0.2. The installed package headers/static-library metadata and delivered
WASM identify libavcodec 63.1.102, agreeing with the n9.0.2 recipe rather than
n8.1.2's 62.28.102. The WASM identifies dav1d 1.5.3-0-gb546257. This is provenance
evidence, not an independent byte-identical rebuild. The exact FFmpeg patch
source selection is inferred from the published build recipe. Preserve the
unchanged original notice alongside this clarification and source materials.

PDF.js 6.4.299
-------------
Package: https://www.npmjs.com/package/pdfjs-dist/v/6.4.299
Source: https://github.com/mozilla/pdf.js
Source tag: https://github.com/mozilla/pdf.js/tree/v6.4.299
pdfjs-LICENSE.txt is copied unchanged from the installed pdfjs-dist package.
The bundled PDF.js JS and worker use Apache-2.0. The reviewed modified worker
is installed from a pinned package archive on a TGDrive GitHub release; see
pdfjs-TGDrive-patch-NOTICE.txt. CMaps, ICC profiles, standard fonts and decoder
WASM are served locally under /static/pdfjs/<upstream-version>/ with their
original resource-directory notices. No external viewer asset CDN is required.

Inter Variable 4.1
------------------
Project: https://rsms.me/inter/
Font source: https://rsms.me/inter/font-files/InterVariable.woff2
License: https://raw.githubusercontent.com/rsms/inter/v4.1/LICENSE.txt
Local asset: /static/fonts/InterVariable.woff2
Font bytes: 352240
SHA-256: 693b77d4f32ee9b8bfc995589b5fad5e99adf2832738661f5402f9978429a8e3
License bytes: 4380
License SHA-256: 262481e844521b326f5ecd053e59b98c8b2da78c8ee1bdbb6e8174305e54935a

The regular variable WOFF2 is bundled unchanged under SIL OFL 1.1.
Inter-OFL-1.1.txt preserves the copyright notice and complete license.
Runtime font requests stay on this TGDrive origin; no font CDN is contacted.

Fraunces 1.000 and Instrument Sans 1.000
---------------------------------------
These four Google Fonts Latin-subset variable WOFF2 files are bundled unchanged.
Byte-for-byte upstream comparisons and embedded SFNT names were inspected on
2026-11-06. FreeType reads the families, versions, copyright and OFL URL;
Fontconfig's character maps confirm Latin coverage. Other scripts use the
existing system fallbacks. Runtime requests remain on this TGDrive origin.

Fraunces project: https://github.com/undercasetype/Fraunces
Embedded version: Version 1.000;[b76b70a41]
Embedded copyright: Copyright 2020 The Fraunces Project Authors
(github.com/undercasetype/Fraunces)
Google Fonts distribution: v38, Latin; optical size 9..144, normal 300..700,
italic 300..600 (the ranges requested by TGDrive's font declarations).
CSS source: https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300..700;1,9..144,300..600&display=swap

Local asset: /static/fonts/fraunces.woff2
Source: https://fonts.gstatic.com/s/fraunces/v38/6NU78FyLNQOQZAnv9bYEvDiIdE9Ea92uemAk_WBq8U_9v0c2Wa0KxC9TeA.woff2
Bytes: 67304
SHA-256: 7234ed860a9cc83045413c4faee63c960a8f2d1917adcf728119307d56e0d783

Local asset: /static/fonts/fraunces-italic.woff2
Source: https://fonts.gstatic.com/s/fraunces/v38/6NU58FyLNQOQZAnv9ZwNjucMHVn85Ni7emAe9lKqZTnbB-gzTK0K1ChjeveQ.woff2
Bytes: 81520
SHA-256: 066710ce7ed235a339d3d6cdcc8b55c0bea5632232662d83aacea25852108271

Fraunces-OFL-1.1.txt preserves the complete SIL OFL 1.1 license and the
upstream notice (Copyright 2018 The Fraunces Project Authors).
Notice source: https://raw.githubusercontent.com/google/fonts/581699fcc0df7715a0f944e28b1a18d38e4aad62/ofl/fraunces/OFL.txt
Notice bytes: 4391
Notice SHA-256: bdf4c22802eaf804f998195871c6b8938aac2ac14b2d78a8bd66a6f1eced833b

Instrument Sans project: https://github.com/Instrument/instrument-sans
Embedded version: Version 1.000;gftools[0.9.28]
Copyright 2022 The Instrument Sans Project Authors
(https://github.com/Instrument/instrument-sans)
Google Fonts distribution: v4, Latin; normal 400..700, italic 400..600.
CSS source: https://fonts.googleapis.com/css2?family=Instrument+Sans:ital,wght@0,400..700;1,400..600&display=swap

Local asset: /static/fonts/instrument.woff2
Source: https://fonts.gstatic.com/s/instrumentsans/v4/pxiTypc9vsFDm051Uf6KVwgkfoSxQ0GsQv8ToedPibnr0SZe1Q.woff2
Bytes: 30092
SHA-256: 2ee17598a98d8a59e4df8152d015bec9ab8e4d5672cc0ab42bef806b568e3971

Local asset: /static/fonts/instrument-italic.woff2
Source: https://fonts.gstatic.com/s/instrumentsans/v4/pxitypc9vsFDm051Uf6KVwgkfoSbSnNPooZAN0lInHGpCWNu15GR.woff2
Bytes: 31828
SHA-256: 77210cdde0281b5ecb0d592e063a98656f1bc36993a3b98f506eb91ff4a433a5

InstrumentSans-OFL-1.1.txt preserves the complete copyright and SIL OFL 1.1.
Notice source: https://raw.githubusercontent.com/google/fonts/2fc9491da70cf3e1bafd3c3da49244c5a496e9d5/ofl/instrumentsans/OFL.txt
Notice bytes: 4403
Notice SHA-256: 9e27a72ed30eb49a08678f6a5d6ed98ec7ba5368f541637ee0683ec9134ef966

React, React DOM and Scheduler
------------------------------
React 19.3.1, React DOM 19.3.1 and Scheduler 0.28.0 are bundled under MIT.
Source project: https://github.com/facebook/react
Published packages: https://www.npmjs.com/package/react/v/19.3.1
https://www.npmjs.com/package/react-dom/v/19.3.1
https://www.npmjs.com/package/scheduler/v/0.28.0

Lucide React 1.58.0
-------------------
Package: https://www.npmjs.com/package/lucide-react/v/1.58.0
Source: https://github.com/lucide-icons/lucide
Its complete notice includes ISC and the MIT notice for Feather-derived icons.

The following license files are copied byte-for-byte from these installed
locked packages. Preserve their copyright and permission notices with the
redistributed browser bundle; minification does not embed those full texts.

react-LICENSE.txt (react 19.3.1)
SHA-256: da6d3703ed11cbe42bd212c725957c98da23cbff1998c05fa4b3d976d1a58e93
react-dom-LICENSE.txt (react-dom 19.3.1)
SHA-256: da6d3703ed11cbe42bd212c725957c98da23cbff1998c05fa4b3d976d1a58e93
scheduler-LICENSE.txt (scheduler 0.28.0)
SHA-256: da6d3703ed11cbe42bd212c725957c98da23cbff1998c05fa4b3d976d1a58e93
lucide-react-LICENSE.txt (lucide-react 1.58.0)
SHA-256: b495047bd93a9b06913511076f504daba17d5bbeb3e0650f3bb53a4220329c57

Mantine UI component foundation
-------------------------------
@mantine/core and @mantine/hooks are pinned together at 9.7.3.
https://mantine.dev/ and https://github.com/mantinedev/mantine
ui-components-LICENSES.txt preserves the library and new dependency license texts,
including the upstream notice for react-remove-scroll-bar whose npm package omits it.
Preserve this file with emitted assets.

Primer Octicons GitHub mark
--------------------------
The GitHub repository link uses mark-github-16 from Primer Octicons.
Source: https://github.com/primer/octicons/blob/main/icons/mark-github-16.svg
License: MIT, GitHub Inc. The complete notice is in octicons-MIT.txt.
