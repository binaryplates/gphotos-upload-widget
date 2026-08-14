// Formatting helpers. Most computation (speed label/css/hint, pct, quota) is
// already done server-side by the D-Bus service — this only covers the
// bits the panel needs to render locally (bytes, percentages, source state).

export function fmtBytes(n) {
    n = Math.max(0, Number(n) || 0);
    const units = [['TB', 1024 ** 4], ['GB', 1024 ** 3], ['MB', 1024 ** 2], ['KB', 1024]];
    for (const [unit, div] of units) {
        if (n >= div || unit === 'KB') {
            const val = n / div;
            if (val >= 100)
                return `${val.toFixed(0)} ${unit}`;
            if (val >= 10)
                return `${val.toFixed(1)} ${unit}`;
            return `${val.toFixed(2)} ${unit}`;
        }
    }
    return `${Math.round(n)} B`;
}

export function fmtPercent(pct) {
    if (pct === null || pct === undefined)
        return '—';
    return `${Math.round(pct)}%`;
}

export function sourceState(item) {
    if (item.cancelled)
        return 'cancelled';
    if (item.paused)
        return 'paused';
    return 'active';
}

export function sourceLabel(item) {
    const parts = String(item.path || '').split('/').filter(Boolean);
    return item.label || parts[parts.length - 1] || item.path || '—';
}
