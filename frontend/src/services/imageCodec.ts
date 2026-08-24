import type { ImageFormatId } from '../utils/imageEnvelope';

export interface EncodedMeshImage {
  blob: Blob;
  format: ImageFormatId;
  width: number;
  height: number;
}

const ACCEPTED_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp', 'image/avif']);

function canvasBlob(
  canvas: HTMLCanvasElement,
  type: string,
  quality: number
): Promise<Blob | null> {
  return new Promise((resolve) => canvas.toBlob(resolve, type, quality));
}

export async function encodeMeshImage(file: File, maxDimension: 64 | 128 | 256 = 256) {
  if (!ACCEPTED_TYPES.has(file.type)) throw new Error('Choose a JPEG, PNG, WebP, or AVIF image.');
  const source = await createImageBitmap(file);
  const scale = Math.min(1, maxDimension / Math.max(source.width, source.height));
  const width = Math.max(1, Math.round(source.width * scale));
  const height = Math.max(1, Math.round(source.height * scale));
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext('2d', { alpha: false });
  if (!context) throw new Error('This browser cannot prepare images.');
  context.drawImage(source, 0, 0, width, height);
  source.close();

  const pixels = context.getImageData(0, 0, width, height);
  for (let offset = 0; offset < pixels.data.length; offset += 4) {
    const luminance = Math.round(
      pixels.data[offset] * 0.299 +
        pixels.data[offset + 1] * 0.587 +
        pixels.data[offset + 2] * 0.114
    );
    pixels.data[offset] = luminance;
    pixels.data[offset + 1] = luminance;
    pixels.data[offset + 2] = luminance;
    pixels.data[offset + 3] = 255;
  }
  context.putImageData(pixels, 0, 0);

  const avif = await canvasBlob(canvas, 'image/avif', 0.2);
  if (avif && avif.type === 'image/avif') return { blob: avif, format: 0, width, height } as const;
  const jpeg = await canvasBlob(canvas, 'image/jpeg', 0.35);
  if (!jpeg) throw new Error('This browser cannot encode the selected image.');
  return { blob: jpeg, format: 1, width, height } as const;
}

/** Edge length the AEIC codec encodes. Fixed: its synthesis network needs a
 *  64x64 latent and collapses below 512. */
export const AEIC_SQUARE_SIZE = 512;

export interface PreparedAeicImage {
  /** Packed 8-bit RGB, `512 * 512 * 3` bytes, row-major. */
  rgb: Uint8Array;
  /** The ORIGINAL photo's dimensions, which travel in the metadata byte so the
   *  receiver can undo the stretch. Not the square's. */
  sourceWidth: number;
  sourceHeight: number;
  /** A browser-rendered preview of exactly what the codec will see. */
  previewBlob: Blob;
}

/**
 * Prepare a photo for the AEIC neural codec: the whole frame stretched into a
 * 512x512 square, as packed RGB.
 *
 * A stretch, not a crop — nothing outside the frame is discarded, and the
 * original aspect ratio is sent alongside so the receiver letterboxes back to
 * it. Colour is kept: unlike the IE4 path, which converts to greyscale to fit an
 * AVIF into a few kilobytes, AEIC's ~150-byte budget is the same either way.
 */
export async function prepareAeicImage(file: File): Promise<PreparedAeicImage> {
  if (!ACCEPTED_TYPES.has(file.type)) throw new Error('Choose a JPEG, PNG, WebP, or AVIF image.');
  const source = await createImageBitmap(file);
  const sourceWidth = source.width;
  const sourceHeight = source.height;
  const canvas = document.createElement('canvas');
  canvas.width = AEIC_SQUARE_SIZE;
  canvas.height = AEIC_SQUARE_SIZE;
  const context = canvas.getContext('2d', { alpha: false });
  if (!context) throw new Error('This browser cannot prepare images.');
  context.drawImage(source, 0, 0, AEIC_SQUARE_SIZE, AEIC_SQUARE_SIZE);
  source.close();

  const pixels = context.getImageData(0, 0, AEIC_SQUARE_SIZE, AEIC_SQUARE_SIZE).data;
  const rgb = new Uint8Array(AEIC_SQUARE_SIZE * AEIC_SQUARE_SIZE * 3);
  for (let i = 0, out = 0; i < pixels.length; i += 4, out += 3) {
    rgb[out] = pixels[i];
    rgb[out + 1] = pixels[i + 1];
    rgb[out + 2] = pixels[i + 2];
  }

  const previewBlob = await canvasBlob(canvas, 'image/png', 1);
  if (!previewBlob) throw new Error('This browser cannot encode the selected image.');
  return { rgb, sourceWidth, sourceHeight, previewBlob };
}
