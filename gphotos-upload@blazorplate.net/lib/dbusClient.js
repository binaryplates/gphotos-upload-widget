import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

Gio._promisify(Gio.DBusProxy, 'new_for_bus', 'new_for_bus_finish');
Gio._promisify(Gio.DBusProxy.prototype, 'call', 'call_finish');

const BUS_NAME = 'net.blazorplate.GPhotosUpload';
const OBJECT_PATH = '/net/blazorplate/GPhotosUpload';
const IFACE = 'net.blazorplate.GPhotosUpload';

const INTROSPECTION_XML = `
<node>
  <interface name="net.blazorplate.GPhotosUpload">
    <method name="Status"><arg type="b" direction="in"/><arg type="s" direction="out"/></method>
    <method name="ServiceStart"><arg type="s" direction="out"/></method>
    <method name="ServiceStop"><arg type="s" direction="out"/></method>
    <method name="ServiceStatus"><arg type="s" direction="out"/></method>
    <method name="SourcesList"><arg type="s" direction="out"/></method>
    <method name="SourcesAdd"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="SourcesRemove"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="SourcesPause"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="SourcesResume"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="SourcesCancel"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="CredentialsGet"><arg type="s" direction="out"/></method>
    <method name="CredentialsSet"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="CredentialsTest"><arg type="s" direction="out"/></method>
    <method name="StorageQuota"><arg type="b" direction="in"/><arg type="s" direction="out"/></method>
    <method name="Reconcile"><arg type="s" direction="out"/></method>
  </interface>
</node>`;

const SIGNATURES = {
    Status: 'b', ServiceStart: '', ServiceStop: '', ServiceStatus: '',
    SourcesList: '', SourcesAdd: 'ss', SourcesRemove: 's', SourcesPause: 's',
    SourcesResume: 's', SourcesCancel: 's', CredentialsGet: '',
    CredentialsSet: 'sss', CredentialsTest: '', StorageQuota: 'b', Reconcile: '',
};

export class DBusClient {
    constructor() {
        this._proxy = null;
        this._proxyPromise = null;
    }

    async _getProxy() {
        if (this._proxy)
            return this._proxy;
        if (!this._proxyPromise) {
            const nodeInfo = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML);
            this._proxyPromise = Gio.DBusProxy.new_for_bus(
                Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, nodeInfo.interfaces[0],
                BUS_NAME, OBJECT_PATH, IFACE, null,
            );
        }
        this._proxy = await this._proxyPromise;
        return this._proxy;
    }

    async call(method, ...args) {
        try {
            const proxy = await this._getProxy();
            const signature = SIGNATURES[method] ?? '';
            const params = new GLib.Variant(`(${signature})`, args);
            const reply = await proxy.call(method, params, Gio.DBusCallFlags.NONE, -1, null);
            const [json] = reply.deep_unpack();
            return JSON.parse(json);
        } catch (e) {
            return { ok: false, error: e.message };
        }
    }
}
