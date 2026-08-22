import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

Gio._promisify(Gio.Subprocess.prototype, 'communicate_utf8_async', 'communicate_utf8_finish');
Gio._promisify(Gio.Subprocess.prototype, 'wait_async', 'wait_finish');

export async function ensureBackend(extension) {
    const extensionPath = extension.path;
    const setupScript = GLib.build_filenamev([extensionPath, 'backend', 'run-setup.sh']);

    if (!GLib.file_test(setupScript, GLib.FileTest.IS_EXECUTABLE))
        return { ok: false, error: 'Extension setup script is missing.' };

    try {
        const proc = Gio.Subprocess.new(
            ['/bin/bash', setupScript, extensionPath],
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
        );
        const [, stderr] = await proc.communicate_utf8_async(null, null);
        await proc.wait_async(null);
        if (!proc.get_successful())
            return { ok: false, error: (stderr || 'Background setup failed.').trim() };
        return { ok: true };
    } catch (e) {
        return { ok: false, error: e.message };
    }
}
