/**
 * HarmonyOS 侧 HDR 源图读取。
 *
 * 关键点（与 Image Kit 的语义对齐）：
 *  1. 必须走 Picture 路径，而不是 createPixelMap；PixelMap 路径会丢掉增益图
 *  2. 解码时指定 desiredDynamicRange = AUTO，否则默认降到 SDR
 *  3. 编码时 needsPackProperties = true 只保证「属性信息」被写入，**不保证**写出
 *     ISO Ultra HDR 的 GContainer XMP + MPF —— 这一点必须实测，不能假设
 *
 * API 基线：@kit.ImageKit API 13+（Picture / AuxiliaryPicture 相关接口）
 */
import { image } from '@kit.ImageKit';
import { fileIo } from '@kit.CoreFileKit';

export enum SourceContainer {
  CUVA = 'cuva',
  ISO_ULTRA_HDR = 'iso_ultrahdr',
  PLAIN = 'plain',
}

export interface HdrSource {
  container: SourceContainer;
  base: image.PixelMap;
  gainMap: image.PixelMap | null;
  /** 源文件字节，供 native 侧 libultrahdr 再次 probe */
  raw: ArrayBuffer;
  notes: string[];
}

async function readAllBytes(uri: string): Promise<ArrayBuffer> {
  const file = fileIo.openSync(uri, fileIo.OpenMode.READ_ONLY);
  try {
    const stat = fileIo.statSync(file.fd);
    const buf = new ArrayBuffer(stat.size);
    fileIo.readSync(file.fd, buf);
    return buf;
  } finally {
    fileIo.closeSync(file);
  }
}

/** 通过 Picture 解码，拿到主图与 GAINMAP 辅助图。 */
export async function readHdrSource(uri: string): Promise<HdrSource> {
  const notes: string[] = [];
  const raw = await readAllBytes(uri);

  const source = image.createImageSource(raw);
  const picture: image.Picture = await source.createPicture();
  const base: image.PixelMap = await picture.getMainPixelmap();

  let gainMap: image.PixelMap | null = null;
  try {
    const aux = await picture.getAuxiliaryPicture(image.AuxiliaryPictureType.GAINMAP);
    if (aux) {
      gainMap = await aux.getPixelMap();
    }
  } catch (err) {
    notes.push(`GAINMAP 辅助图读取失败: ${JSON.stringify(err)}`);
  }

  let container = SourceContainer.PLAIN;
  if (gainMap) {
    container = SourceContainer.CUVA;
    notes.push('检测到 GAINMAP 辅助图；若源为华为 HDR Vivid，其增益图语义与 hdrgm 不等价');
  }

  const info = await source.getImageInfo();
  notes.push(`源格式=${info.mimeType ?? 'unknown'} 主图=${base.getImageInfoSync().size.width}x${base.getImageInfoSync().size.height}`);

  await source.release();
  await picture.release();

  return { container, base, gainMap, raw, notes };
}

/**
 * 若业务允许「先看 HDR 合成效果」，可直接取合成后的 PixelMap 做预览；
 * 但导出时必须用未合成的 base + gainMap 分别处理。
 */
export async function composeHdrPreview(uri: string): Promise<image.PixelMap | null> {
  const raw = await readAllBytes(uri);
  const source = image.createImageSource(raw);
  const picture: image.Picture = await source.createPicture();
  try {
    return await picture.getHdrComposedPixelmap();
  } catch (err) {
    return null;
  } finally {
    await source.release();
    await picture.release();
  }
}
