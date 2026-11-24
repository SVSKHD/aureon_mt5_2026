// EMA cloud: the band between two EMAs (e.g. EMA20 / EMA50), green while the fast EMA is above the slow one, red
// otherwise. A Lightweight Charts custom series: it draws only the visible range, one fill per colour per frame
// (two Path2D objects), so it stays cheap at any zoom. Values come from the Python engine; nothing is computed here.
import {
  customSeriesDefaultOptions,
  type CustomData,
  type CustomSeriesOptions,
  type CustomSeriesPricePlotValues,
  type ICustomSeriesPaneRenderer,
  type ICustomSeriesPaneView,
  type PaneRendererCustomData,
  type PriceToCoordinateConverter,
  type Time,
  type WhitespaceData,
} from "lightweight-charts";
import type { CanvasRenderingTarget2D } from "fancy-canvas";

export interface CloudData extends CustomData<Time> {
  fast: number;
  slow: number;
}

export interface CloudOptions extends CustomSeriesOptions {
  bullColor: string;
  bearColor: string;
}

const defaults: CloudOptions = {
  ...customSeriesDefaultOptions,
  bullColor: "rgba(38, 166, 154, 0.18)",
  bearColor: "rgba(239, 83, 80, 0.18)",
  priceLineVisible: false,
  lastValueVisible: false,
} as CloudOptions;

class CloudRenderer implements ICustomSeriesPaneRenderer {
  data: PaneRendererCustomData<Time, CloudData> | null = null;
  options: CloudOptions = defaults;

  draw(target: CanvasRenderingTarget2D, toY: PriceToCoordinateConverter): void {
    const d = this.data;
    if (!d || !d.visibleRange || d.bars.length < 2) return;
    target.useBitmapCoordinateSpace(({ context: ctx, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
      const bull = new Path2D();
      const bear = new Path2D();
      const from = Math.max(1, d.visibleRange!.from);
      const to = Math.min(d.bars.length - 1, d.visibleRange!.to + 1);
      for (let i = from; i <= to; i++) {
        const a = d.bars[i - 1], b = d.bars[i];
        const ad = a.originalData, bd = b.originalData;
        if (ad.fast === undefined || bd.fast === undefined) continue;
        const y = (v: number) => (toY(v) ?? 0) * vr;
        const p = (bd.fast >= bd.slow ? bull : bear);
        p.moveTo(a.x * hr, y(ad.fast));
        p.lineTo(b.x * hr, y(bd.fast));
        p.lineTo(b.x * hr, y(bd.slow));
        p.lineTo(a.x * hr, y(ad.slow));
        p.closePath();
      }
      ctx.fillStyle = this.options.bullColor;
      ctx.fill(bull);
      ctx.fillStyle = this.options.bearColor;
      ctx.fill(bear);
    });
  }
}

export class CloudView implements ICustomSeriesPaneView<Time, CloudData, CloudOptions> {
  private r = new CloudRenderer();
  renderer(): ICustomSeriesPaneRenderer {
    return this.r;
  }
  update(data: PaneRendererCustomData<Time, CloudData>, options: CloudOptions): void {
    this.r.data = data;
    this.r.options = options;
  }
  priceValueBuilder(d: CloudData): CustomSeriesPricePlotValues {
    return [Math.min(d.fast, d.slow), Math.max(d.fast, d.slow), d.fast];
  }
  isWhitespace(d: CloudData | WhitespaceData<Time>): d is WhitespaceData<Time> {
    return (d as CloudData).fast === undefined;
  }
  defaultOptions(): CloudOptions {
    return defaults;
  }
  destroy(): void {}
}
