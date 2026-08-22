export class UploadActions {
    constructor(client) {
        this._client = client;
    }

    serviceStart() { return this._client.call('ServiceStart'); }
    serviceStop() { return this._client.call('ServiceStop'); }
    serviceStatus() { return this._client.call('ServiceStatus'); }
    reconcile() { return this._client.call('Reconcile'); }
    storageQuota(force = false) { return this._client.call('StorageQuota', force); }
    credentialsGet() { return this._client.call('CredentialsGet'); }
    credentialsTest() { return this._client.call('CredentialsTest'); }
    dateFixScan() { return this._client.call('DateFixScan'); }
    dateFixApply() { return this._client.call('DateFixApply'); }
    dateFixStatus() { return this._client.call('DateFixStatus'); }
    dateFixCancel() { return this._client.call('DateFixCancel'); }
    credentialsSet({ clientId, clientSecret, apiKey }) {
        return this._client.call('CredentialsSet', clientId || '', clientSecret || '', apiKey || '');
    }
}
