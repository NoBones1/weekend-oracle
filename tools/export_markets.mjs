// Copies HIP3 Lens's "what does each perp track" table into markets.json.
// Reads hip3/src/data/underlying.ts and never writes to it, so the Oracle
// uses the same curated mapping as HIP3 Lens without changing that code.
// Run: node --no-warnings tools/export_markets.mjs
import { writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = path.resolve(here, "../../hip3/src/data/underlying.ts");
const m = await import(src);

const out = {};
for (const [base, map] of Object.entries(m.UNDERLYING)) {
  out[base] = {
    symbol: map.symbol ?? null,
    kind: map.kind,
    name: map.name,
    venue: m.venueFor(map),
    note: map.note ?? null,
  };
}
const file = path.resolve(here, "../markets.json");
writeFileSync(file, JSON.stringify({ source: "hip3/src/data/underlying.ts", exported: new Date().toISOString(), markets: out }, null, 1) + "\n");
console.log(`wrote ${Object.keys(out).length} markets to ${file}`);
