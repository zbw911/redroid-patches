package com.buildploy.statusbarprototype;

import android.app.Application;
import android.app.BroadcastOptions;
import android.content.BroadcastReceiver;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.graphics.Rect;
import android.hardware.display.DisplayManager;
import android.os.Binder;
import android.os.Handler;
import android.os.Looper;
import android.os.RemoteException;
import android.os.SystemClock;
import android.util.Log;
import android.view.Display;
import com.android.internal.statusbar.RegisterStatusBarResult;
import java.lang.reflect.Array;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.util.HashMap;
import java.util.Map;
import java.util.UUID;

/** Observe real SystemUI state while retaining the original global CommandQueue endpoint. */
public final class AppearanceBridge {
    private static final String HOST = "com.buildploy.statusbarprobe.host";
    private static final String ACTION = "com.buildploy.statusbarprobe.APPEARANCE";
    private static final String CONTROL = "com.buildploy.statusbarprobe.CONTROL";
    private static final String PERMISSION = "android.permission.STATUS_BAR_SERVICE";
    private static final String CALLBACK_SOURCE = "SystemUI.CommandQueue.onSystemBarAttributesChanged";
    private static final String SNAPSHOT_SOURCE = "SystemUI.StatusBarManagerService.registerStatusBarForAllDisplays";
    private static final String EPOCH = UUID.randomUUID().toString();
    // These maps are confined to the SystemUI main handler, including startup snapshots.
    private static final Map<Integer, Snapshot> snapshots = new HashMap<>();
    private static final Map<Integer, Binding> bindings = new HashMap<>();
    private static Application application;
    private static Handler main;
    private static DisplayManager displays;

    /** Only the existing-task replay loop calls this wrapper; new-task callbacks stay untouched. */
    public static void existingTaskAppeared(Object organizer, Object info) throws Throwable {
        Method original = Class.forName("com.android.wm.shell.ShellTaskOrganizer")
                .getDeclaredMethod("onTaskAppeared", Class.forName("android.window.TaskAppearedInfo"));
        original.setAccessible(true);
        try {
            original.invoke(organizer, info);
        } catch (InvocationTargetException failure) {
            // Preserve the original listener's exception instead of swallowing/wrapping it.
            throw failure.getCause();
        }
        long identity = Binder.clearCallingIdentity();
        try {
            restoreVisiblePreviewTask(info);
        } catch (Throwable failure) {
            // Recovery is supplemental: failures cannot interrupt the original Shell startup.
            warn(failure);
        } finally {
            Binder.restoreCallingIdentity(identity);
        }
    }

    private static Application currentApplication() throws ReflectiveOperationException {
        return (Application) Class.forName("android.app.ActivityThread")
                .getMethod("currentApplication").invoke(null);
    }

    private static void restoreVisiblePreviewTask(Object info) throws Exception {
        Object task = info.getClass().getMethod("getTaskInfo").invoke(info);
        if (!(Boolean) task.getClass().getField("isVisible").get(task)) return;
        int displayId = task.getClass().getField("displayId").getInt(task);
        if (displayId <= 0) return;
        Application app = currentApplication();
        if (app == null) return;
        DisplayManager manager = app.getSystemService(DisplayManager.class);
        Display display = manager.getDisplay(displayId);
        int virtualType = Display.class.getField("TYPE_VIRTUAL").getInt(null);
        if (display == null || !display.isValid()
                || !display.getName().startsWith("buildploy-preview-")
                || (Integer) Display.class.getMethod("getType").invoke(display) != virtualType) return;
        int trusted = Display.class.getField("FLAG_TRUSTED").getInt(null);
        int owner = (Integer) Display.class.getMethod("getOwnerUid").invoke(display);
        if ((display.getFlags() & trusted) == 0 || (owner != 0 && owner != 1000)) return;
        String unique = (String) Display.class.getMethod("getUniqueId").invoke(display);
        Object leash = info.getClass().getMethod("getLeash").invoke(info);
        Class<?> surface = Class.forName("android.view.SurfaceControl");
        if (leash == null || !(Boolean) surface.getMethod("isValid").invoke(leash)) return;
        Display current = manager.getDisplay(displayId);
        if (current == null || !current.isValid()
                || !unique.equals(Display.class.getMethod("getUniqueId").invoke(current))) return;
        Class<?> transactionClass = Class.forName("android.view.SurfaceControl$Transaction");
        // Organizer death migrates an existing Task to a hidden Surface. There is no OPEN
        // transition to show it again; restore only the visible task in our trusted preview VD.
        try (AutoCloseable transaction = (AutoCloseable) transactionClass.getConstructor().newInstance()) {
            transactionClass.getMethod("show", surface).invoke(transaction, leash);
            transactionClass.getMethod("apply").invoke(transaction);
        }
        Log.i("BuildployAppearanceBridge", "existing-visible-preview-task-restored display=" + displayId);
    }

    /** Replace the original single registration call, not an additional registration or observer. */
    public static RegisterStatusBarResult register(Object service, Object commandQueue)
            throws RemoteException {
        final Map<?, ?> states;
        try {
            Class<?> api = Class.forName("com.android.internal.statusbar.IStatusBarService");
            Class<?> callback = Class.forName("com.android.internal.statusbar.IStatusBar");
            states = (Map<?, ?>) api.getMethod("registerStatusBarForAllDisplays", callback)
                    .invoke(service, commandQueue);
        } catch (InvocationTargetException failure) {
            Throwable cause = failure.getCause();
            if (cause instanceof RemoteException) throw (RemoteException) cause;
            if (cause instanceof RuntimeException) throw (RuntimeException) cause;
            throw new IllegalStateException("SystemUI registration failed", cause);
        } catch (ReflectiveOperationException failure) {
            throw new IllegalStateException("Frozen SystemUI registration API unavailable", failure);
        }
        // This is the same default-display result that the original SystemUI startup consumes.
        RegisterStatusBarResult result = (RegisterStatusBarResult) states.get("0");
        if (result == null) throw new IllegalStateException("Missing default display registration state");
        try {
            if (initialize()) {
                for (Map.Entry<?, ?> entry : states.entrySet()) {
                    int displayId = Integer.parseInt((String) entry.getKey());
                    if (displayId <= 0) continue;
                    Object state = entry.getValue();
                    Snapshot snapshot = snapshot(displayId,
                            state.getClass().getField("mAppearance").getInt(state),
                            state.getClass().getField("mAppearanceRegions").get(state),
                            (String) state.getClass().getField("mPackageName").get(state),
                            SNAPSHOT_SOURCE);
                    if (snapshot != null) main.post(() -> {
                        // Live callbacks can arrive while registration is returning its snapshot.
                        // Never replace a callback already queued during that round trip.
                        if (!snapshots.containsKey(snapshot.displayId)) record(snapshot);
                    });
                }
                main.post(() -> send(new Intent().putExtra("state", "bridge-ready")));
            }
        } catch (Throwable failure) {
            warn(failure);
        }
        return result;
    }

    public static void dispatch(int displayId, int appearance, Object regions,
            boolean navbarColorManagedByIme, int behavior, int requestedVisibleTypes,
            String packageName, Object letterboxDetails) {
        if (displayId <= 0) return;
        long identity = Binder.clearCallingIdentity();
        try {
            if (!initialize()) return;
            Snapshot snapshot = snapshot(displayId, appearance, regions, packageName, CALLBACK_SOURCE);
            if (snapshot != null) main.post(() -> record(snapshot));
        } catch (Throwable failure) {
            // An observer failure must never stop the original callback or its message dispatch.
            warn(failure);
        } finally {
            Binder.restoreCallingIdentity(identity);
        }
    }

    private static synchronized boolean initialize() throws Exception {
        if (application != null) return true;
        Application app = currentApplication();
        if (app == null) return false;
        Handler handler = new Handler(Looper.getMainLooper());
        DisplayManager manager = app.getSystemService(DisplayManager.class);
        app.registerReceiver(new BroadcastReceiver() {
            @Override public void onReceive(Context context, Intent intent) {
                try {
                    int sender = getSentFromUid();
                    String senderPackage = getSentFromPackage();
                    if (!HOST.equals(senderPackage) || sender < 0
                            || app.getPackageManager().getApplicationInfo(HOST, 0).uid != sender
                            || app.getPackageManager().checkSignatures(sender, app.getApplicationInfo().uid)
                                    != PackageManager.SIGNATURE_MATCH) return;
                    control(intent);
                } catch (Throwable failure) { warn(failure); }
            }
        }, new IntentFilter(CONTROL), PERMISSION, handler, Context.RECEIVER_EXPORTED);
        manager.registerDisplayListener(new DisplayManager.DisplayListener() {
            @Override public void onDisplayAdded(int displayId) { }
            @Override public void onDisplayChanged(int displayId) {
                Binding binding = bindings.get(displayId);
                if (binding != null && !binding.uniqueId.equals(uniqueId(displayId))) clear(displayId);
            }
            @Override public void onDisplayRemoved(int displayId) { clear(displayId); }
        }, handler);
        displays = manager;
        main = handler;
        application = app;
        return true;
    }

    private static void clear(int displayId) {
        snapshots.remove(displayId);
        bindings.remove(displayId);
    }

    private static String uniqueId(int displayId) {
        try {
            Display display = displays.getDisplay(displayId);
            return display == null ? null : (String) Display.class.getMethod("getUniqueId").invoke(display);
        } catch (ReflectiveOperationException failure) { return null; }
    }

    private static Snapshot snapshot(int displayId, int appearance, Object regions,
            String packageName, String source) throws Exception {
        String uniqueId = uniqueId(displayId);
        if (uniqueId == null || packageName == null) return null;
        int count = regions == null ? 0 : Array.getLength(regions);
        if (count > 64) throw new IllegalArgumentException("Too many appearance regions");
        int[] appearances = new int[count], bounds = new int[count * 4];
        for (int i = 0; i < count; i++) {
            Object region = Array.get(regions, i);
            appearances[i] = (Integer) region.getClass().getMethod("getAppearance").invoke(region);
            Rect rect = (Rect) region.getClass().getMethod("getBounds").invoke(region);
            bounds[i * 4] = rect.left; bounds[i * 4 + 1] = rect.top;
            bounds[i * 4 + 2] = rect.right; bounds[i * 4 + 3] = rect.bottom;
        }
        return new Snapshot(displayId, uniqueId, appearance, packageName, source,
                appearances, bounds, SystemClock.elapsedRealtimeNanos(), SystemClock.elapsedRealtime());
    }

    private static void record(Snapshot snapshot) {
        try {
            if (!snapshot.uniqueId.equals(uniqueId(snapshot.displayId))) return;
            Snapshot current = snapshots.get(snapshot.displayId);
            if (current != null && current.uniqueId.equals(snapshot.uniqueId)
                    && snapshot.sequence <= current.sequence) return;
            snapshots.put(snapshot.displayId, snapshot);
            Binding binding = bindings.get(snapshot.displayId);
            if (binding != null) reply(binding, false);
        } catch (Throwable failure) { warn(failure); }
    }

    private static void control(Intent intent) {
        if (!CONTROL.equals(intent.getAction())) return;
        Binding binding = new Binding(intent);
        if (!binding.uniqueId.equals(uniqueId(binding.displayId))) {
            send(binding.intent().putExtra("state", "pending").putExtra("reason", "DISPLAY_IDENTITY_MISMATCH"));
            return;
        }
        String operation = intent.getStringExtra("op");
        if ("bind".equals(operation)) {
            bindings.put(binding.displayId, binding);
            reply(binding, true);
        } else if ("unbind".equals(operation)) {
            Binding current = bindings.get(binding.displayId);
            if (current != null && binding.same(current)) bindings.remove(binding.displayId);
            send(binding.intent().putExtra("state", "unbound"));
        } else throw new IllegalArgumentException("Invalid appearance operation");
    }

    private static void reply(Binding binding, boolean replayed) {
        Snapshot snapshot = snapshots.get(binding.displayId);
        Intent intent = binding.intent().putExtra("replayed", replayed);
        if (snapshot == null || !binding.uniqueId.equals(snapshot.uniqueId)
                || !binding.packageName.equals(snapshot.packageName)) {
            send(intent.putExtra("state", "pending").putExtra("reason", "NO_MATCHING_SYSTEMUI_STATE"));
            return;
        }
        send(intent.putExtra("state", "ready").putExtra("appearance", snapshot.appearance)
                .putExtra("appearancePackageName", snapshot.packageName)
                .putExtra("seq", snapshot.sequence).putExtra("eventTimeElapsedRealtime", snapshot.time)
                .putExtra("source", snapshot.source).putExtra("regionAppearances", snapshot.appearances)
                .putExtra("regionBounds", snapshot.bounds));
    }

    private static void send(Intent intent) {
        try {
            intent.setAction(ACTION).setComponent(new ComponentName(HOST, HOST + ".AppearanceEvents"))
                    .putExtra("bridgeEpoch", EPOCH);
            application.sendBroadcast(intent, PERMISSION,
                    BroadcastOptions.makeBasic().setShareIdentityEnabled(true).toBundle());
        } catch (Throwable failure) { warn(failure); }
    }

    private static void warn(Throwable failure) {
        Log.w("BuildployAppearanceBridge", "bridge-error:" + failure.getClass().getSimpleName());
    }

    private static String text(Intent intent, String key, int maximum) {
        String value = intent.getStringExtra(key);
        if (value == null || value.isEmpty() || value.length() > maximum)
            throw new IllegalArgumentException("Invalid binding field");
        return value;
    }

    private static final class Binding {
        final int displayId;
        final String uniqueId, packageName, runtimeId, generation, requestId;
        Binding(Intent intent) {
            displayId = intent.getIntExtra("displayId", -1);
            if (displayId <= 0) throw new IllegalArgumentException("Invalid display");
            uniqueId = text(intent, "displayUniqueId", 256);
            packageName = text(intent, "packageName", 255);
            runtimeId = text(intent, "runtimeId", 128);
            generation = text(intent, "generation", 128);
            requestId = text(intent, "requestId", 128);
            if (!generation.matches("[A-Za-z0-9_-]{1,128}"))
                throw new IllegalArgumentException("Invalid generation");
        }
        boolean same(Binding other) {
            return displayId == other.displayId && uniqueId.equals(other.uniqueId)
                    && packageName.equals(other.packageName) && runtimeId.equals(other.runtimeId)
                    && generation.equals(other.generation) && requestId.equals(other.requestId);
        }
        Intent intent() {
            return new Intent().putExtra("displayId", displayId).putExtra("displayUniqueId", uniqueId)
                    .putExtra("packageName", packageName).putExtra("runtimeId", runtimeId)
                    .putExtra("generation", generation).putExtra("requestId", requestId);
        }
    }

    private static final class Snapshot {
        final int displayId, appearance;
        final String uniqueId, packageName, source;
        final int[] appearances, bounds;
        final long sequence, time;
        Snapshot(int displayId, String uniqueId, int appearance, String packageName, String source,
                int[] appearances, int[] bounds, long sequence, long time) {
            this.displayId = displayId; this.uniqueId = uniqueId; this.appearance = appearance;
            this.packageName = packageName; this.source = source;
            this.appearances = appearances; this.bounds = bounds;
            this.sequence = sequence; this.time = time;
        }
    }
}
