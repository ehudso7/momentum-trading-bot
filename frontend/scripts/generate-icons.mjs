/**
 * Generate binary brand assets from the canonical SVG mark.
 *
 * Outputs:
 *   src/app/favicon.ico                 multi-size ICO (16/32/48)
 *   src/app/apple-icon.png              180×180 full-bleed
 *   public/icons/icon-192.png           PWA manifest icon
 *   public/icons/icon-512.png           PWA manifest icon
 *   public/icons/icon-512-maskable.png  full-bleed Android maskable icon
 *
 * Usage: node scripts/generate-icons.mjs
 * Re-run whenever src/app/icon.svg changes.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import sharp from "sharp";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const iconSvg = readFileSync(join(root, "src/app/icon.svg"));

/**
 * Full-bleed variant used by platforms that apply their own icon mask.
 * The brand intentionally uses a restrained operations palette with no glow,
 * neon, or gradient treatment.
 */
function fullBleedSvg(glyphScale) {
  const inset = (512 * (1 - glyphScale)) / 2;
  return Buffer.from(`<svg width="512" height="512" viewBox="0 0 512 512" fill="none" xmlns="http://www.w3.org/2000/svg">
  <rect width="512" height="512" fill="#151918"/>
  <g transform="translate(${inset} ${inset}) scale(${glyphScale})">
    <g fill="#6F837A">
      <rect x="122" y="280" width="22" height="126" rx="6"/>
      <rect x="236" y="224" width="22" height="160" rx="6"/>
      <rect x="350" y="160" width="22" height="166" rx="6"/>
    </g>
    <path d="M96 386 L188 294 L254 344 L346 252 L416 182" stroke="#D9E3DD" stroke-width="34" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M416 182 L393 260 L337 204 Z" fill="#D9E3DD"/>
  </g>
</svg>`);
}

async function png(svg, size) {
  return sharp(svg, { density: 300 }).resize(size, size).png().toBuffer();
}

/** Pack PNG buffers into an ICO container. */
function packIco(entries) {
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(entries.length, 4);

  const dir = Buffer.alloc(16 * entries.length);
  let offset = 6 + dir.length;
  entries.forEach(({ size, data }, i) => {
    const base = i * 16;
    dir.writeUInt8(size >= 256 ? 0 : size, base + 0);
    dir.writeUInt8(size >= 256 ? 0 : size, base + 1);
    dir.writeUInt8(0, base + 2);
    dir.writeUInt8(0, base + 3);
    dir.writeUInt16LE(1, base + 4);
    dir.writeUInt16LE(32, base + 6);
    dir.writeUInt32LE(data.length, base + 8);
    dir.writeUInt32LE(offset, base + 12);
    offset += data.length;
  });

  return Buffer.concat([header, dir, ...entries.map((entry) => entry.data)]);
}

async function main() {
  const icoSizes = [16, 32, 48];
  const icoEntries = await Promise.all(
    icoSizes.map(async (size) => ({ size, data: await png(iconSvg, size) }))
  );

  writeFileSync(join(root, "src/app/favicon.ico"), packIco(icoEntries));
  writeFileSync(join(root, "src/app/apple-icon.png"), await png(fullBleedSvg(0.84), 180));
  writeFileSync(join(root, "public/icons/icon-192.png"), await png(iconSvg, 192));
  writeFileSync(join(root, "public/icons/icon-512.png"), await png(iconSvg, 512));
  writeFileSync(
    join(root, "public/icons/icon-512-maskable.png"),
    await png(fullBleedSvg(0.66), 512)
  );

  console.log("✓ brand icon set regenerated");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
