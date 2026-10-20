export type TableColumn<T extends Record<string, unknown> = Record<string, unknown>> = {
  key: string;
  label: string;
  sortable?: boolean;
  numeric?: boolean;
  format?: (value: unknown, row: T) => string;
  compare?: (a: T, b: T) => number;
};
