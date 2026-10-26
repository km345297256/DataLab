import type { NumericRange, ViewSelection } from './viewContext';
export interface PlotlyExtent { x: NumericRange; y?: NumericRange; coordinateSystem: ViewSelection['coordinateSystem'] }
/** Only numerical extents leave Plotly; no points or source values are published. */
export function bindPlotlyInteractions(element: HTMLElement, extent: PlotlyExtent, change: (selection: ViewSelection | undefined, zoom?: {x:NumericRange;y?:NumericRange}) => void) {
  type EventElement = HTMLElement & { on?: (name:string,callback:(event:any)=>void)=>void; removeListener?: (name:string,callback:(event:any)=>void)=>void };
  if(!element)return()=>{};
  const target=element as EventElement;
  const range=(raw:unknown,limits:NumericRange):NumericRange|undefined=>{
    if(!Array.isArray(raw)||raw.length!==2||!raw.every(n=>typeof n==='number'&&Number.isFinite(n)))return;
    const lo=Math.min(...limits),hi=Math.max(...limits);return [Math.max(lo,Math.min(hi,Math.min(...raw))),Math.max(lo,Math.min(hi,Math.max(...raw)))];
  };
  const selected=(event:any)=>{const x=range(event?.range?.x??(Array.isArray(event?.points)&&event.points.length?[Math.min(...event.points.map((p:any)=>p.x)),Math.max(...event.points.map((p:any)=>p.x))]:null),extent.x);if(!x)return;const y=extent.y?range(event?.range?.y,extent.y):undefined;change({kind:'range',coordinateSystem:extent.coordinateSystem,bounds:'inclusive',x,...(y?{y}:{})});};
  const relayout=(event:any)=>{if(event?.['xaxis.autorange']===true){change(undefined);return;}const x=range([event?.['xaxis.range[0]'],event?.['xaxis.range[1]']],extent.x);if(!x)return;const y=extent.y?range([event?.['yaxis.range[0]'],event?.['yaxis.range[1]']],extent.y):undefined;change({kind:'range',coordinateSystem:extent.coordinateSystem,bounds:'inclusive',x,...(y?{y}:{})},{x,...(y?{y}:{})});};
  target.on?.('plotly_selected',selected);target.on?.('plotly_relayout',relayout);
  return ()=>{target.removeListener?.('plotly_selected',selected);target.removeListener?.('plotly_relayout',relayout);};
}
