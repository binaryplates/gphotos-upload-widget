import GLib from 'gi://GLib';

export class StatusRunner {
    constructor(client, settings) {
        this._client = client;
        this._settings = settings;
        this._timeoutId = 0;
    }

    startPolling(onUpdate) {
        this._onUpdate = onUpdate;
        this.refreshNow();
        this._scheduleNext();
    }

    stopPolling() {
        if (this._timeoutId) {
            GLib.source_remove(this._timeoutId);
            this._timeoutId = 0;
        }
    }

    _scheduleNext() {
        this.stopPolling();
        const seconds = this._settings.get_int('refresh-seconds') || 5;
        this._timeoutId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, seconds, () => {
            this.refreshNow();
            this._timeoutId = 0;
            this._scheduleNext();
            return GLib.SOURCE_REMOVE;
        });
    }

    async refreshNow(force = false) {
        const payload = await this._client.call('Status', force);
        this._onUpdate?.(payload);
        return payload;
    }
}
