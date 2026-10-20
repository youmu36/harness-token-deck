// Canonical JSON compares IPC payloads independently of object property order.
export function canonicalPayload(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(canonicalPayload).join(',') + ']';
  if (value && typeof value === 'object') {
    return '{' + Object.entries(value).filter(([, item]) => item !== undefined)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([name, item]) => JSON.stringify(name) + ':' + canonicalPayload(item)).join(',') + '}';
  }
  return JSON.stringify(value);
}
