// 1-bit bitmap packing for the 384-dot print head: row-major, MSB first, 48 bytes per row.
export const WIDTH = 384;
export const ROW_BYTES = 48;
export const MAX_ROWS = 2048;
export const ALPHA_INK_MIN = 96;

/** Pack the alpha channel of an RGBA buffer (alpha >= ALPHA_INK_MIN is ink). */
export function packAlpha(rgba, width, rows) {
  const rowBytes = Math.ceil(width / 8);
  const out = new Uint8Array(rowBytes * rows);
  for (let y = 0; y < rows; y += 1) {
    for (let x = 0; x < width; x += 1) {
      if (rgba[(y * width + x) * 4 + 3] >= ALPHA_INK_MIN) {
        out[y * rowBytes + (x >> 3)] |= 0x80 >> (x & 7);
      }
    }
  }
  return out;
}

/** Pack one 0/1 byte per pixel (WIDTH pixels per row). */
export function packInk(ink, rows) {
  const out = new Uint8Array(ROW_BYTES * rows);
  for (let y = 0; y < rows; y += 1) {
    for (let x = 0; x < WIDTH; x += 1) {
      if (ink[y * WIDTH + x]) out[y * ROW_BYTES + (x >> 3)] |= 0x80 >> (x & 7);
    }
  }
  return out;
}

/** Black ink on an opaque white background; reuses `target` when it is given. */
export function unpack(bitmap, rows, target) {
  const image = target ?? new ImageData(WIDTH, rows);
  const data = image.data;
  for (let y = 0; y < rows; y += 1) {
    for (let x = 0; x < WIDTH; x += 1) {
      const ink = (bitmap[y * ROW_BYTES + (x >> 3)] >> (7 - (x & 7))) & 1;
      const value = ink ? 0 : 255;
      const i = (y * WIDTH + x) * 4;
      data[i] = value;
      data[i + 1] = value;
      data[i + 2] = value;
      data[i + 3] = 255;
    }
  }
  return image;
}

/** Share of ink dots, 0..100. */
export function inkPercent(bitmap) {
  if (!bitmap.length) return 0;
  let ink = 0;
  for (let i = 0; i < bitmap.length; i += 1) {
    let v = bitmap[i];
    while (v) {
      v &= v - 1;
      ink += 1;
    }
  }
  return (ink * 100) / (bitmap.length * 8);
}

const CHUNK = 0x8000;

/** One btoa over the whole binary string: per-chunk btoa would pad mid-string. */
export function bytesToBase64(bytes) {
  let binary = '';
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}
