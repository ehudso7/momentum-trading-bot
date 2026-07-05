/**
 * Generate binary brand assets from the canonical SVG mark.
 *
 * Outputs:
 *   src/app/favicon.ico              multi-size ICO (16/32/48, PNG-compressed entries)
 *   src/app/apple-icon.png           180×180 full-bleed (iOS applies its own corner mask)
 *   public/icons/icon-192.png        PWA manifest icon
 *   public/icons/icon-512.png        PWA manifest icon
 *   public/icons/icon-512-maskable.png  full-bleed with safe-zone padding for Android masks
 *
 * Usage: node scripts/generate-icons.mjs  (or `npm run generate:icons`)
 * Idempotent — re-run whenever src/app/icon.svg changes.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import sharp from "sharp";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const iconSvg = readFileSync(join(root, "src/app/icon.svg"));

/**
 * Full-bleed variant: square background (no corner radius — the platform
 * applies its own mask) with the glyph scaled into the safe zone.
 * Gradients use userSpaceOnUse coordinates, so they scale with the group.
 */
function fullBleedSvg(glyphScale) {
  const inset = (512 * (1 - glyphScale)) / 2;
  return Buffer.from(`<svg width="512" height="512" viewBox="0 0 512 512" fill="none" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="mf-arrow" x1="96" y1="416" x2="424" y2="180" gradientUnits="userSpaceOnUse">
      <stop stop-color="#06b6d4"/>
      <stop offset="1" stop-color="#8b5cf6"/>
    </linearGradient>
    <linearGradient id="mf-bars" x1="118" y1="440" x2="394" y2="116" gradientUnits="userSpaceOnUse">
      <stop stop-color="#0891b2"/>
      <stop offset="1" stop-color="#7c3aed"/>
    </linearGradient>
    <radialGradient id="mf-glow" cx="0.72" cy="0.28" r="0.9">
      <stop stop-color="#8b5cf6" stop-opacity="0.22"/>
      <stop offset="0.55" stop-color="#06b6d4" stop-opacity="0.08"/>
      <stop offset="1" stop-color="#0a0b14" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="512" height="512" fill="#0a0b14"/>
  <rect width="512" height="512" fill="url(#mf-glow)"/>
  <g transform="translate(${inset} ${inset}) scale(${glyphScale})">
    <g opacity="0.38" fill="url(#mf-bars)">
      <rect x="139" y="268" width="12" height="172" rx="6"/>
      <rect x="118" y="296" width="54" height="112" rx="14"/>
      <rect x="250" y="196" width="12" height="192" rx="6"/>
      <rect x="229" y="224" width="54" height="136" rx="14"/>
      <rect x="361" y="116" width="12" height="216" rx="6"/>
      <rect x="340" y="144" width="54" height="156" rx="14"/>
    </g>
    <path d="M100 400 L200 300 L256 356 L346 266" stroke="url(#mf-arrow)" stroke-width="40" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M424 180 L395 293 L311 209 Z" fill="url(#mf-arrow)"/>
  </g>
</svg>`);
}

async function png(svg, size) {
  return sharp(svg, { density: 300 }).resize(size, size).png().toBuffer();
}

/** Pack PNG buffers into an ICO container (PNG entries are valid since Vista). */
function packIco(entries) {
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0); // reserved
  header.writeUInt16LE(1, 2); // type: icon
  header.writeUInt16LE(entries.length, 4);

  const dir = Buffer.alloc(16 * entries.length);
  let offset = 6 + dir.length;
  entries.forEach(({ size, data }, i) => {
    const base = i * 16;
    dir.writeUInt8(size >= 256 ? 0 : size, base + 0); // width
    dir.writeUInt8(size >= 256 ? 0 : size, base + 1); // height
    dir.writeUInt8(0, base + 2); // palette
    dir.writeUInt8(0, base + 3); // reserved
    dir.writeUInt16LE(1, base + 4); // color planes
    dir.writeUInt16LE(32, base + 6); // bits per pixel
    dir.writeUInt32LE(data.length, base + 8);
    dir.writeUInt32LE(offset, base + 12);
    offset += data.length;
  });

  return Buffer.concat([header, dir, ...entries.map((e) => e.data)]);
}

async function main() {
  const icoSizes = [16, 32, 48];
  const icoEntries = await Promise.all(
    icoSizes.map(async (size) => ({ size, data: await png(iconSvg, size) }))
  );
  writeFileSync(join(root, "src/app/favicon.ico"), packIco(icoEntries));
  console.log("✓ src/app/favicon.ico (16/32/48)");

  writeFileSync(
    join(root, "src/app/apple-icon.png"),
    await png(fullBleedSvg(0.84), 180)
  );
  console.log("✓ src/app/apple-icon.png (180×180)");

  writeFileSync(join(root, "public/icons/icon-192.png"), await png(iconSvg, 192));
  writeFileSync(join(root, "public/icons/icon-512.png"), await png(iconSvg, 512));
  console.log("✓ public/icons/icon-192.png, public/icons/icon-512.png");

  writeFileSync(
    join(root, "public/icons/icon-512-maskable.png"),
    await png(fullBleedSvg(0.66), 512)
  );
  console.log("✓ public/icons/icon-512-maskable.png (safe-zone padded)");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
