# Notes for Claude

## Keep the wiring diagrams in step with the HAL

Each Mesa card has a wiring diagram, published as a Claude artifact and linked from README.md under
*Wiring Diagrams*:

| Card  | Source in the repo                | Published page |
|-------|-----------------------------------|----------------|
| 7I96S | `docs/wiring/7i96s-wiring.html`   | https://claude.ai/artifact/QtuukSyugFAVWwX2BSeY3Q |
| 7I84U | `docs/wiring/7i84-wiring.html`    | https://claude.ai/artifact/7kCnsSy715uRvjh2P8u7yT |
| 7I83  | `docs/wiring/7i83-wiring.html`    | https://claude.ai/artifact/6iFcG6nCMmkbU8jrhAMgzb |

**Any change to which physical pin something uses must be followed by an update to the matching
diagram(s).** That covers changes in `Mesa7I96S.hal` or `custom_postgui.hal` that add, remove or move a
`net` to an `hm2_7i96s.0.*` pin (stepgen, encoder, ssr, 7i84 input/output, 7i83 analogout/analogena).
It also covers `-not` / `-invert` changes, `output_source` / `output_sink` masks and `[HOSTMOT2] CONFIG`.
A move between cards (like the laser going from the 7I96S to the 7I84) means updating both pages.

To update one: edit its file in `docs/wiring/`, starting with the data at the top of its script
(`USED`, `AXES`, `ENC` and the like), plus any header chips, legend, pin-table rows and checks that
mention the pin. Then publish that file to its **same URL** (the Artifact tool with `url` set), so the
README links keep working, and commit the file with the HAL change. The repo copy is the source: if the
published page was changed elsewhere, read it with the Artifact tool first and merge it into the file.

Also keep `VISION_HARDWARE.md` and the README's *Hardware Configuration* section in step when they
describe the same wiring.

Ground the diagrams in the card itself as well as the HAL. `mesaflash --device 7i96s --addr 10.10.10.10
--readhmid` (read-only) prints the firmware pinout, which is how we know the smart-serial cards are on
P1 (port 1) and not TB2.
