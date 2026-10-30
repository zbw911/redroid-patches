package com.buildploy.statusbarprobe.host;

import android.app.Application;
import android.app.BroadcastOptions;
import android.content.Context;
import android.content.BroadcastReceiver;
import android.content.IntentFilter;
import android.content.Intent;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Insets;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.os.Binder;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.util.AtomicFile;
import android.util.DisplayMetrics;
import android.util.Log;
import android.view.Display;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowManager;
import java.io.File;
import java.io.FileNotFoundException;
import java.io.FileOutputStream;
import java.io.IOException;
import java.lang.reflect.Array;
import java.lang.reflect.Constructor;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.FutureTask;
import java.util.concurrent.TimeUnit;
import org.json.JSONArray;
import org.json.JSONObject;

/** Persistent system APK owns windows; root preview helper remains the only display owner. */
public final class StatusBarHost extends Application implements DisplayManager.DisplayListener {
    static final String TAG = "BuildployStatusBarHost";
    static final String CONTROL = "com.buildploy.statusbarprobe.CONTROL";
    private final Map<Integer, Screen> screens = new LinkedHashMap<>();
    private final Handler main = new Handler(Looper.getMainLooper());
    private DisplayManager displays;
    private AtomicFile bindings;
    private String storageError;

    @Override public void onCreate() {
        super.onCreate();
        displays = getSystemService(DisplayManager.class);
        bindings = new AtomicFile(new File(createDeviceProtectedStorageContext().getNoBackupFilesDir(), "bindings-v1.json"));
        displays.registerDisplayListener(this, main);
        restore();
        IntentFilter removed = new IntentFilter(Intent.ACTION_PACKAGE_REMOVED); removed.addDataScheme("package");
        registerReceiver(new BroadcastReceiver() {
            @Override public void onReceive(Context context, Intent intent) {
                if (intent.getBooleanExtra(Intent.EXTRA_REPLACING, false) || intent.getData() == null) return;
                String pkg = intent.getData().getSchemeSpecificPart();
                for (Screen screen : new ArrayList<>(screens.values())) if (pkg.equals(screen.binding.optString("packageName"))) {
                    try { remove(screen); } catch (Exception failure) { Log.e(TAG, "PACKAGE_CLEANUP_FAILED", failure); }
                }
            }
        }, removed, Context.RECEIVER_EXPORTED);
        new Thread(this::serve, "statusbar-host-control").start();
    }

    private static int height(Display display) {
        DisplayMetrics metrics = new DisplayMetrics(); display.getRealMetrics(metrics);
        return Math.max(1, Math.round(28f * metrics.densityDpi / 160));
    }

    private int bootCount() throws Exception {
        int value = Settings.Global.getInt(getContentResolver(), Settings.Global.BOOT_COUNT);
        if (value < 0) throw new IllegalStateException("BOOT_IDENTITY_UNAVAILABLE");
        return value;
    }

    private static String uniqueId(Display display) throws Exception {
        return (String) Display.class.getMethod("getUniqueId").invoke(display);
    }

    private JSONObject identity(JSONObject request) throws Exception {
        String runtime = request.getString("runtimeId"), generation = request.getString("generation");
        String pkg = request.getString("packageName"), unique = request.getString("displayUniqueId");
        if (!runtime.matches("[A-Za-z0-9_-]{1,128}") || !generation.matches("[A-Za-z0-9_-]{1,128}")
                || !pkg.matches("[A-Za-z][A-Za-z0-9_]*(?:\\.[A-Za-z][A-Za-z0-9_]*)+") || unique.length() > 1024 || unique.isEmpty())
            throw new IllegalArgumentException("INVALID_BINDING_IDENTITY");
        return new JSONObject().put("runtimeId", runtime).put("generation", generation).put("packageName", pkg)
                .put("displayId", request.getInt("displayId")).put("displayUniqueId", unique);
    }

    private Display validate(JSONObject binding, boolean restored) throws Exception {
        int id = binding.getInt("displayId");
        Display display = displays.getDisplay(id);
        if (id <= 0 || display == null || !display.isValid() || !binding.getString("displayUniqueId").equals(uniqueId(display)))
            throw new IllegalStateException("DISPLAY_IDENTITY_EXPIRED");
        String prefix = "buildploy-preview-" + binding.getString("runtimeId") + "-" + binding.getString("generation") + "-";
        int owner = (Integer) Display.class.getMethod("getOwnerUid").invoke(display);
        if (!display.getName().startsWith(prefix) || (owner != 0 && owner != 1000))
            throw new IllegalArgumentException("PREVIEW_DISPLAY_OWNER_REQUIRED");
        int uid = getPackageManager().getApplicationInfo(binding.getString("packageName"), 0).uid;
        int boot = bootCount();
        if (restored && (binding.getInt("bootCount") != boot || binding.getInt("packageUid") != uid
                || binding.getInt("ownerUid") != owner || !binding.getString("displayName").equals(display.getName())))
            throw new IllegalStateException("PERSISTED_BINDING_EXPIRED");
        if (!restored) binding.put("bootCount", boot).put("packageUid", uid).put("ownerUid", owner).put("displayName", display.getName());
        return display;
    }

    private static boolean same(JSONObject a, JSONObject b) throws Exception {
        for (String field : new String[]{"runtimeId", "generation", "packageName", "displayId", "displayUniqueId"})
            if (!a.get(field).equals(b.get(field))) return false;
        return true;
    }

    private void persist() throws Exception {
        JSONArray values = new JSONArray();
        for (Screen screen : screens.values()) values.put(screen.binding);
        FileOutputStream stream = null;
        try {
            stream = bindings.startWrite();
            stream.write(new JSONObject().put("version", 1).put("bindings", values).toString().getBytes(StandardCharsets.UTF_8));
            bindings.finishWrite(stream);
            storageError = null;
        } catch (Exception failure) {
            if (stream != null) bindings.failWrite(stream);
            storageError = failure.getClass().getSimpleName();
            throw failure;
        }
    }

    private void restore() {
        try {
            JSONObject saved = new JSONObject(new String(bindings.readFully(), StandardCharsets.UTF_8));
            if (saved.getInt("version") != 1) throw new IllegalStateException("BINDING_STORAGE_VERSION");
            JSONArray values = saved.getJSONArray("bindings");
            for (int i = 0; i < values.length(); i++) {
                Screen screen = null;
                try {
                    JSONObject value = values.getJSONObject(i);
                    identity(value);
                    Display display = validate(value, true);
                    if (screens.containsKey(display.getDisplayId())) throw new IllegalStateException("DUPLICATE_BINDING");
                    screen = new Screen(display, value); screen.add();
                    screens.put(display.getDisplayId(), screen);
                } catch (Exception expired) {
                    Log.i(TAG, "BINDING_NOT_RESTORED " + expired.getClass().getSimpleName());
                }
            }
            persist();
            for (Screen screen : screens.values()) replay(screen, "bind");
        } catch (FileNotFoundException empty) {
            // First boot has no registrations. It must not adopt displays from another helper.
        } catch (Exception failure) {
            storageError = failure.getClass().getSimpleName();
            Log.e(TAG, "BINDING_RESTORE_FAILED", failure);
        }
    }

    private JSONObject execute(JSONObject request) throws Exception {
        String action = request.getString("action");
        if (request.has("appearance") || request.has("darkIcons")) throw new IllegalArgumentException("MANUAL_APPEARANCE_DISABLED");
        if ("hello".equals(action)) return new JSONObject().put("hostPackage", getPackageName()).put("heightDp", 28);
        if ("status".equals(action)) {
            JSONArray bars = new JSONArray();
            for (Screen screen : screens.values()) bars.put(screen.status());
            return new JSONObject().put("hostPid", android.os.Process.myPid()).put("hostUid", android.os.Process.myUid())
                    .put("bars", bars).put("storageError", storageError == null ? JSONObject.NULL : storageError)
                    .put("createsVirtualDisplays", false).put("registersGlobalStatusBar", false);
        }
        if (!"bind".equals(action) && !"release".equals(action)) throw new IllegalArgumentException("UNKNOWN_ACTION");
        JSONObject value = identity(request);
        int id = value.getInt("displayId");
        Screen current = screens.get(id);
        if (current != null && !same(current.binding, value)) throw new IllegalStateException("BINDING_CONFLICT");
        if ("release".equals(action)) {
            if (current != null) remove(current);
            return new JSONObject().put("released", true);
        }
        Display display = validate(value, false);
        for (Screen other : screens.values()) if (other != current && (other.binding.getString("runtimeId").equals(value.getString("runtimeId"))
                || other.binding.getString("packageName").equals(value.getString("packageName")))) throw new IllegalStateException("BINDING_CONFLICT");
        if (current == null) {
            current = new Screen(display, value); current.add(); screens.put(id, current);
            try { persist(); }
            catch (Exception failure) { screens.remove(id); current.remove(); throw failure; }
            replay(current, "bind");
        } else {
            validate(current.binding, true); current.resize();
            if (storageError != null) persist();
            replay(current, "bind");
        }
        return current.status().put("registered", true);
    }

    private void remove(Screen screen) throws Exception {
        int id = screen.display.getDisplayId(); screens.remove(id);
        try { persist(); }
        catch (Exception failure) { screens.put(id, screen); throw failure; }
        replay(screen, "unbind");
        screen.remove();
    }

    private void replay(Screen screen, String operation) throws Exception {
        Intent intent = new Intent(CONTROL).setPackage("com.android.systemui").putExtra("op", operation)
                .putExtra("displayId", screen.display.getDisplayId()).putExtra("requestId", screen.requestId);
        for (String field : new String[]{"runtimeId", "generation", "packageName", "displayUniqueId"}) intent.putExtra(field, screen.binding.getString(field));
        BroadcastOptions options = BroadcastOptions.makeBasic().setShareIdentityEnabled(true);
        sendBroadcast(intent, "android.permission.STATUS_BAR_SERVICE", options.toBundle());
    }

    JSONObject appearance(Intent intent, int senderUid) throws Exception {
        if (!AppearanceEvent.ACTION.equals(intent.getAction())) throw new IllegalArgumentException("INVALID_EVENT_ACTION");
        String state = intent.getStringExtra("state");
        if ("bridge-ready".equals(state)) {
            for (Screen screen : new ArrayList<>(screens.values())) {
                try {
                    validate(screen.binding, true);
                    screen.requestId = java.util.UUID.randomUUID().toString();
                    screen.lastReceived = null;
                    replay(screen, "bind");
                } catch (Exception expired) { remove(screen); }
            }
            return new JSONObject().put("rebound", screens.size());
        }
        Screen screen = screens.get(intent.getIntExtra("displayId", -1));
        if (screen == null) return new JSONObject().put("applied", false).put("reason", "DISPLAY_NOT_MANAGED");
        for (String field : new String[]{"runtimeId", "generation", "packageName", "displayUniqueId"})
            if (!screen.binding.getString(field).equals(intent.getStringExtra(field))) throw new IllegalArgumentException("EVENT_BINDING_MISMATCH");
        if (!screen.requestId.equals(intent.getStringExtra("requestId"))) throw new IllegalArgumentException("EVENT_NONCE_MISMATCH");
        validate(screen.binding, true);
        if ("pending".equals(state)) return new JSONObject().put("applied", false).put("reason", "AWAITING_SYSTEMUI_STATE");
        if (!"ready".equals(state)) throw new IllegalArgumentException("UNEXPECTED_BRIDGE_STATE");
        AppearanceEvent event = new AppearanceEvent(intent, senderUid);
        if (screen.lastReceived != null && event.bridgeEpoch.equals(screen.lastReceived.bridgeEpoch) && event.seq <= screen.lastReceived.seq)
            return new JSONObject().put("applied", false).put("reason", "STALE_EVENT");
        screen.lastReceived = event; screen.latestSupported = event.supported;
        if (event.supported) {
            screen.lastApplied = event; screen.appliedAt = android.os.SystemClock.elapsedRealtime();
            screen.bar.dark = event.darkIcons; screen.bar.invalidate();
        }
        return new JSONObject().put("applied", event.supported).put("event", event.json()).put("state", screen.status());
    }

    @Override public void onDisplayAdded(int id) { /* Only an explicit root binding may create a bar. */ }
    @Override public void onDisplayRemoved(int id) {
        Screen screen = screens.get(id);
        if (screen != null) try { remove(screen); }
        catch (Exception failure) { Log.e(TAG, "DISPLAY_CLEANUP_FAILED", failure); main.postDelayed(() -> onDisplayRemoved(id), 1000); }
    }
    @Override public void onDisplayChanged(int id) {
        Screen screen = screens.get(id);
        if (screen != null) try { validate(screen.binding, true); screen.resize(); }
        catch (Exception failure) { Log.e(TAG, "DISPLAY_REFRESH_FAILED", failure); onDisplayRemoved(id); }
    }

    private void serve() {
        try (LocalServerSocket server = new LocalServerSocket("buildploy-statusbar-host-v1")) {
            while (true) try (LocalSocket socket = server.accept()) {
                if (socket.getPeerCredentials().getUid() != 0) continue;
                socket.setSoTimeout(3000);
                StringBuilder line = new StringBuilder();
                int next;
                while ((next = socket.getInputStream().read()) != -1 && next != '\n') {
                    if (line.length() >= 16384) throw new IOException("CONTROL_TOO_LARGE");
                    if (next > 127) throw new IOException("ASCII_IDENTITY_REQUIRED");
                    line.append((char) next);
                }
                final String encoded = line.toString();
                FutureTask<JSONObject> task = new FutureTask<>(() -> {
                    JSONObject result;
                    try { result = execute(new JSONObject(encoded)).put("ok", true); }
                    catch (Exception failure) { result = new JSONObject().put("ok", false).put("error", failure.getMessage() == null ? failure.getClass().getSimpleName() : failure.getMessage()); }
                    return result.put("protocolVersion", 1);
                });
                main.post(task);
                JSONObject result = task.get(5, TimeUnit.SECONDS);
                socket.getOutputStream().write((result + "\n").getBytes(StandardCharsets.UTF_8));
            } catch (Exception failure) { Log.w(TAG, "CONTROL_REQUEST_FAILED " + failure.getClass().getSimpleName()); }
        } catch (IOException failure) { throw new IllegalStateException("HOST_CONTROL_UNAVAILABLE", failure); }
    }

    private final class Screen {
        final Display display;
        int height;
        final JSONObject binding;
        String requestId = java.util.UUID.randomUUID().toString();
        final Binder insetOwner = new Binder();
        final Bar bar;
        final WindowManager manager;
        final WindowManager.LayoutParams params;
        AppearanceEvent lastReceived, lastApplied;
        boolean latestSupported;
        long appliedAt;
        Screen(Display display, JSONObject binding) throws Exception {
            this.display = display;
            this.binding = binding;
            this.height = height(display);
            Context dc = StatusBarHost.this.createDisplayContext(display);
            Context wc = dc.createWindowContext(WindowManager.LayoutParams.TYPE_STATUS_BAR, null);
            manager = wc.getSystemService(WindowManager.class);
            bar = new Bar(wc, this);
            params = new WindowManager.LayoutParams(-1, height, WindowManager.LayoutParams.TYPE_STATUS_BAR,
                    WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                            | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN | WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS,
                    PixelFormat.TRANSLUCENT);
            params.gravity = Gravity.TOP;
            params.packageName = getPackageName();
            params.token = new Binder();
            params.setTitle("BuildployStatusBarHost-" + display.getDisplayId());
            params.setFitInsetsTypes(0);
            params.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_ALWAYS;
            updateInsets();
        }
        void updateInsets() throws Exception {
            Class<?> providerClass = Class.forName("android.view.InsetsFrameProvider");
            Constructor<?> constructor = providerClass.getConstructor(Object.class, int.class, int.class);
            Object providers = Array.newInstance(providerClass, 3);
            int[] types = { WindowInsets.Type.statusBars(), WindowInsets.Type.tappableElement(), WindowInsets.Type.mandatorySystemGestures() };
            for (int index = 0; index < types.length; index++) {
                Object provider = constructor.newInstance(insetOwner, 0, types[index]);
                providerClass.getMethod("setInsetsSize", Insets.class).invoke(provider, Insets.of(0, height, 0, 0));
                Array.set(providers, index, provider);
            }
            WindowManager.LayoutParams.class.getField("providedInsets").set(params, providers);
        }
        void resize() throws Exception {
            int nextHeight = height(display);
            if (nextHeight == height) return;
            height = nextHeight; params.height = height; updateInsets();
            manager.updateViewLayout(bar, params); bar.invalidate();
        }
        void add() { manager.addView(bar, params); }
        void remove() { manager.removeViewImmediate(bar); }
        JSONObject status() throws Exception {
            return new JSONObject().put("displayId", display.getDisplayId()).put("displayValid", display.isValid())
                    .put("binding", new JSONObject(binding.toString())).put("requestId", requestId)
                    .put("appearanceReady", lastApplied != null).put("barExists", true).put("attached", bar.isAttachedToWindow()).put("visibility", bar.getVisibility())
                    .put("windowVisibility", bar.getWindowVisibility()).put("shown", bar.isShown())
                    .put("requestedHeightPx", height).put("widthPx", bar.getWidth()).put("heightPx", bar.getHeight())
                    .put("viewId", bar.getId()).put("windowType", params.type).put("darkIcons", bar.dark)
                    .put("inkSource", lastApplied == null ? "pending-systemui-event" : latestSupported ? "systemui-event" : "previous-supported-systemui-event")
                    .put("latestAppearanceSupported", latestSupported).put("appliedAtElapsedRealtime", appliedAt)
                    .put("lastReceivedEvent", lastReceived == null ? JSONObject.NULL : lastReceived.json())
                    .put("lastAppliedEvent", lastApplied == null ? JSONObject.NULL : lastApplied.json())
                    .put("lastDrawnSequence", bar.lastDrawnSequence).put("lastDrawnEventTimeElapsedRealtime", bar.lastDrawnEventTime)
                    .put("lastDrawnAtElapsedRealtime", bar.lastDrawnAt)
                    .put("iconContent", "static-prototype")
                    .put("visibilityScope", "View state only; not a SurfaceFlinger visibility assertion")
                    .put("providedInsets", new JSONArray().put("statusBars").put("tappableElement").put("mandatorySystemGestures"))
                    .put("providedTopInsetPx", height);
        }
    }

    private static final class Bar extends View {
        boolean dark;
        final Screen screen;
        long lastDrawnSequence = -1, lastDrawnEventTime = -1, lastDrawnAt;
        final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        Bar(Context context, Screen screen) {
            super(context); this.screen = screen;
            setId(View.generateViewId());
            setWillNotDraw(false);
            setContentDescription("Static prototype status bar; icon ink follows authenticated SystemUI appearance");
        }
        @Override protected void onDraw(Canvas canvas) {
            if (screen.lastApplied == null) return;
            canvas.save();
            float scale = screen.height / 28f;
            canvas.scale(scale, scale);
            float width = getWidth() / scale, height = getHeight() / scale;
            paint.setColor(dark ? Color.BLACK : Color.WHITE);
            paint.setTextSize(16);
            paint.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
            canvas.drawText("12:00", 12, height - 7, paint);
            canvas.drawRect(width - 46, 9, width - 42, height - 8, paint);
            canvas.drawRect(width - 39, 6, width - 35, height - 8, paint);
            canvas.drawRect(width - 32, 3, width - 28, height - 8, paint);
            paint.setStyle(Paint.Style.STROKE); paint.setStrokeWidth(2);
            canvas.drawRect(width - 22, 7, width - 8, height - 8, paint);
            paint.setStyle(Paint.Style.FILL);
            canvas.drawRect(width - 20, 9, width - 13, height - 10, paint);
            canvas.restore();
            if (screen.lastApplied != null && (lastDrawnSequence != screen.lastApplied.seq || lastDrawnEventTime != screen.lastApplied.time)) {
                lastDrawnSequence = screen.lastApplied.seq;
                lastDrawnEventTime = screen.lastApplied.time;
                lastDrawnAt = android.os.SystemClock.elapsedRealtime();
                try {
                    android.util.Log.i(TAG, "BUILDPLOY_STATUSBAR_DRAW " + new JSONObject()
                            .put("displayId", screen.display.getDisplayId()).put("seq", lastDrawnSequence)
                            .put("drawnAtElapsedRealtime", lastDrawnAt).put("darkIcons", dark).put("event", screen.lastApplied.json()));
                } catch (Exception ignored) { /* Only primitive prototype metadata is serialized. */ }
            }
        }
    }
}
