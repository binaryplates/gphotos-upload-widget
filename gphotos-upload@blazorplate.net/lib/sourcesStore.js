export class SourcesStore {
    constructor(client) {
        this._client = client;
    }

    list() { return this._client.call('SourcesList'); }
    add(path, album) { return this._client.call('SourcesAdd', path, album || ''); }
    remove(path) { return this._client.call('SourcesRemove', path); }
    pause(path) { return this._client.call('SourcesPause', path); }
    resume(path) { return this._client.call('SourcesResume', path); }
    cancel(path) { return this._client.call('SourcesCancel', path); }
}
