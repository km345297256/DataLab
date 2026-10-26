import { numericColumns, parseTable, type PreviewTable } from './data';

// These are the tabular reader's display budgets, separate from the basic CSV viewer.
export const PLOTLY_ROWS = 200;
export const PLOTLY_COLUMNS = 100;
export const MAX_ROW_OFFSET = 100000;
export const MAX_COLUMN_OFFSET = 16383;

export interface PlotlyTableWindow extends PreviewTable {
  rowOffset: number;
  columnOffset: number;
  totalRows: number | null;
  totalColumns: number;
  hasMoreRows: boolean;
  hasMoreColumns: boolean;
  partial: boolean;
}

const integer = (value: unknown, minimum: number, maximum: number): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum && value <= maximum;

export function parsePlotlyTableWindow(value: unknown, expected: { rowOffset: number; columnOffset: number }): PlotlyTableWindow {
  const table = parseTable(value);
  const source = value as Record<string, unknown>;
  const rowOffset = source.row_offset, columnOffset = source.column_offset;
  const totalRows = source.total_rows, totalColumns = source.total_columns;
  if (table.rows.length > PLOTLY_ROWS || table.columns.length > PLOTLY_COLUMNS
    || !integer(rowOffset, 0, MAX_ROW_OFFSET) || !integer(columnOffset, 0, MAX_COLUMN_OFFSET)
    || rowOffset !== expected.rowOffset || columnOffset !== expected.columnOffset
    || !integer(totalColumns, 1, MAX_COLUMN_OFFSET + 1)
    || table.columns.length !== Math.min(PLOTLY_COLUMNS, totalColumns - columnOffset)
    || !(totalRows === null ? table.rows.length === PLOTLY_ROWS : integer(totalRows, rowOffset + table.rows.length, Number.MAX_SAFE_INTEGER))
    || (rowOffset > 0 && !table.rows.length)) throw new Error('绘图窗口与请求范围不一致，请重新读取。');
  const checkedTotalRows = totalRows as number | null;
  const hasMoreRows = checkedTotalRows === null || rowOffset + table.rows.length < checkedTotalRows;
  const hasMoreColumns = columnOffset + table.columns.length < totalColumns;
  return { ...table, rowOffset, columnOffset, totalRows: checkedTotalRows, totalColumns, hasMoreRows, hasMoreColumns,
    partial: rowOffset > 0 || columnOffset > 0 || hasMoreRows || hasMoreColumns };
}

/** Keep column identity across row pages and overlapping column windows. */
export function preservePlotlyColumns(previous: Pick<PlotlyTableWindow, 'columnOffset'> | undefined, next: PlotlyTableWindow, x: number, ys: number[]) {
  const numeric = numericColumns(next);
  if (!previous) return { x: -1, ys: numeric.slice(0, 1) };
  const local = (index: number) => previous.columnOffset + index - next.columnOffset;
  const nextX = x < 0 ? -1 : local(x);
  // Preserve a chosen column even if this page contains only missing values.
  const selected = ys.map(local).filter(index => index >= 0 && index < next.columns.length).slice(0, 8);
  return { x: nextX >= 0 && nextX < next.columns.length ? nextX : -1,
    ys: selected.length || !ys.length ? selected : numeric.slice(0, 1) };
}

export function plotlyWindowOptions(rowStart: number, columnStart: number) {
  if (!integer(rowStart, 1, MAX_ROW_OFFSET + 1) || !integer(columnStart, 1, MAX_COLUMN_OFFSET + 1)) {
    throw new Error('请输入有效的起始数据行（1–100001）和起始列（1–16384）。');
  }
  return { row_offset: rowStart - 1, column_offset: columnStart - 1 };
}
