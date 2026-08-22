"""D-Bus service: net.blazorplate.GPhotosUpload on the session bus.

Started on demand via D-Bus activation (see net.blazorplate.GPhotosUpload.service).
Exits after a period of no incoming calls so it doesn't linger forever —
the bus will just start it again next time something calls a method.
"""
from __future__ import annotations

import json

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from gphotos_upload_service import logic  # noqa: E402

BUS_NAME = "net.blazorplate.GPhotosUpload"
OBJECT_PATH = "/net/blazorplate/GPhotosUpload"
IDLE_EXIT_SECONDS = 15 * 60

INTROSPECTION_XML = """
<node>
  <interface name="net.blazorplate.GPhotosUpload">
    <method name="Status">
      <arg type="b" name="force" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="ServiceStart">
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="ServiceStop">
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="ServiceStatus">
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="SourcesList">
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="SourcesAdd">
      <arg type="s" name="path" direction="in"/>
      <arg type="s" name="album" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="SourcesRemove">
      <arg type="s" name="path" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="SourcesPause">
      <arg type="s" name="path" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="SourcesResume">
      <arg type="s" name="path" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="SourcesCancel">
      <arg type="s" name="path" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="CredentialsGet">
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="CredentialsSet">
      <arg type="s" name="client_id" direction="in"/>
      <arg type="s" name="client_secret" direction="in"/>
      <arg type="s" name="api_key" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="CredentialsTest">
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="StorageQuota">
      <arg type="b" name="force" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="Reconcile">
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="DateFixScan">
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="DateFixApply">
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="DateFixStatus">
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="DateFixCancel">
      <arg type="s" name="result" direction="out"/>
    </method>
  </interface>
</node>
"""

# Method name -> (logic function, arg names in call order)
_METHODS = {
    "Status": (logic.status, ["force"]),
    "ServiceStart": (logic.service_start, []),
    "ServiceStop": (logic.service_stop, []),
    "ServiceStatus": (logic.service_status, []),
    "SourcesList": (logic.sources_list, []),
    "SourcesAdd": (logic.sources_add, ["path", "album"]),
    "SourcesRemove": (logic.sources_remove, ["path"]),
    "SourcesPause": (logic.sources_pause, ["path"]),
    "SourcesResume": (logic.sources_resume, ["path"]),
    "SourcesCancel": (logic.sources_cancel, ["path"]),
    "CredentialsGet": (logic.credentials_get, []),
    "CredentialsSet": (logic.credentials_set, ["client_id", "client_secret", "api_key"]),
    "CredentialsTest": (logic.credentials_test, []),
    "StorageQuota": (logic.storage_quota, ["force"]),
    "Reconcile": (logic.reconcile, []),
    "DateFixScan": (logic.datefix_scan, []),
    "DateFixApply": (logic.datefix_apply, []),
    "DateFixStatus": (logic.datefix_status, []),
    "DateFixCancel": (logic.datefix_cancel, []),
}


class Service:
    def __init__(self):
        self._loop = GLib.MainLoop()
        self._idle_source = 0
        self._owner_id = 0

    def run(self) -> int:
        self._owner_id = Gio.bus_own_name(
            Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
            self._on_bus_acquired, None, self._on_name_lost,
        )
        self._reset_idle_timer()
        self._loop.run()
        Gio.bus_unown_name(self._owner_id)
        return 0

    def _on_name_lost(self, connection, name):
        self._loop.quit()

    def _on_bus_acquired(self, connection, name):
        node_info = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
        connection.register_object(
            OBJECT_PATH, node_info.interfaces[0], self._handle_call, None, None,
        )

    def _reset_idle_timer(self):
        if self._idle_source:
            GLib.source_remove(self._idle_source)
        self._idle_source = GLib.timeout_add_seconds(IDLE_EXIT_SECONDS, self._on_idle_timeout)

    def _on_idle_timeout(self):
        self._loop.quit()
        return GLib.SOURCE_REMOVE

    def _handle_call(self, connection, sender, object_path, interface_name, method_name, params, invocation):
        self._reset_idle_timer()
        entry = _METHODS.get(method_name)
        if entry is None:
            invocation.return_error_literal(Gio.dbus_error_quark(), Gio.DBusError.UNKNOWN_METHOD, f"Unknown method {method_name}")
            return
        func, arg_names = entry
        args = list(params.unpack())
        kwargs = dict(zip(arg_names, args))
        try:
            result = func(**kwargs)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        invocation.return_value(GLib.Variant("(s)", (json.dumps(result, default=str),)))
