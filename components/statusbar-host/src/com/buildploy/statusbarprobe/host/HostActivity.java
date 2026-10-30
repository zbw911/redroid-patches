package com.buildploy.statusbarprobe.host;

import android.app.Activity;
import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Insets;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.os.Binder;
import android.os.Bundle;
import android.os.Looper;
import android.view.Display;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.widget.TextView;
import java.lang.reflect.Array;
import java.lang.reflect.Constructor;
import java.util.LinkedHashMap;
import java.util.Map;
import org.json.JSONArray;
import org.json.JSONObject;

/** Isolated privileged APK process owns secondary-display prototype windows, never a VD. */
public final class HostActivity extends Activity {
    static HostActivity current;
    private final Map<Integer, Screen> screens = new LinkedHashMap<>();
    private TextView info;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        current = this;
        info = new TextView(this);
        info.setTextSize(18);
        info.setPadding(24, 64, 24, 24);
        info.setBackgroundColor(Color.rgb(240, 232, 210));
        info.setTextColor(Color.BLACK);
        setContentView(info);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        refresh();
    }

    JSONObject command(String action, int displayId, int height) throws Exception {
        if (Looper.myLooper() != Looper.getMainLooper()) throw new IllegalStateException("MAIN_THREAD_REQUIRED");
        if ("status".equals(action) && displayId == -1) return status();
        if (displayId <= 0) throw new IllegalArgumentException("SECONDARY_DISPLAY_REQUIRED");
        Screen screen = screens.get(displayId);
        switch (action) {
            case "add":
                if (screen != null) throw new IllegalStateException("BAR_ALREADY_ADDED");
                if (height < 1 || height > 160) throw new IllegalArgumentException("INVALID_HEIGHT_PIXELS");
                Display display = getSystemService(DisplayManager.class).getDisplay(displayId);
                if (display == null || !display.isValid()) throw new IllegalArgumentException("DISPLAY_UNAVAILABLE");
                screen = new Screen(display, height);
                screen.add();
                screens.put(displayId, screen);
                break;
            case "remove":
                if (screen != null) { screen.remove(); screens.remove(displayId); }
                refresh();
                return new JSONObject().put("displayId", displayId).put("removed", true);
            case "status":
                if (screen == null) return new JSONObject().put("displayId", displayId).put("barExists", false);
                break;
            default: throw new IllegalArgumentException("UNKNOWN_ACTION");
        }
        refresh();
        return screen.status();
    }

    JSONObject appearance(AppearanceEvent event) throws Exception {
        if (Looper.myLooper() != Looper.getMainLooper()) throw new IllegalStateException("MAIN_THREAD_REQUIRED");
        Screen screen = screens.get(event.displayId);
        if (screen == null) return new JSONObject().put("applied", false).put("reason", "DISPLAY_NOT_MANAGED").put("event", event.json());
        if (screen.lastReceived != null && (event.time < screen.lastReceived.time
                || event.time == screen.lastReceived.time && event.seq <= screen.lastReceived.seq)) {
            return new JSONObject().put("applied", false).put("reason", "STALE_EVENT").put("event", event.json());
        }
        screen.lastReceived = event;
        screen.latestSupported = event.supported;
        if (event.supported) {
            screen.lastApplied = event;
            screen.appliedAt = android.os.SystemClock.elapsedRealtime();
            screen.bar.dark = event.darkIcons;
            screen.bar.invalidate();
        }
        return new JSONObject().put("applied", event.supported)
                .put("reason", event.supported ? "UNIFORM_SYSTEMUI_APPEARANCE" : "MIXED_REGIONS_UNSUPPORTED")
                .put("event", event.json()).put("state", screen.status());
    }

    private JSONObject status() throws Exception {
        JSONArray bars = new JSONArray();
        for (Screen screen : screens.values()) bars.put(screen.status());
        return new JSONObject().put("hostPid", android.os.Process.myPid()).put("hostUid", android.os.Process.myUid())
                .put("activityDisplayId", getDisplay().getDisplayId()).put("bars", bars)
                .put("createsVirtualDisplays", false).put("registersGlobalStatusBar", false);
    }

    private void refresh() {
        if (info != null) info.setText("ISOLATED STATUS BAR WINDOW HOST\n\n"
                + "Normal APK process, default display\nPID=" + android.os.Process.myPid() + " UID=" + android.os.Process.myUid()
                + "\nManaged secondary displays: " + screens.keySet()
                + "\n\nTransparent TYPE_STATUS_BAR + real providedInsets."
                + "\nClock/icons are STATIC PROTOTYPE MATERIAL. Ink follows authenticated SystemUI events."
                + "\nNo global IStatusBar registration. No virtual display creation."
                + "\nClosing this Activity removes all owned bars."
                + "\n\nEXPERIMENT ONLY: signature-permission-protected receivers. Mixed regions are unsupported.");
    }

    @Override public void onDestroy() {
        for (Screen screen : screens.values()) {
            try { screen.remove(); }
            catch (RuntimeException failure) { android.util.Log.e(Commands.TAG, "STATUSBAR_HOST_CLEANUP_FAILED " + failure.getClass().getSimpleName()); }
        }
        screens.clear();
        if (current == this) current = null;
        super.onDestroy();
    }

    private final class Screen {
        final Display display;
        final int height;
        final Bar bar;
        final WindowManager manager;
        final WindowManager.LayoutParams params;
        AppearanceEvent lastReceived, lastApplied;
        boolean latestSupported;
        long appliedAt;
        Screen(Display display, int height) throws Exception {
            this.display = display;
            this.height = height;
            Context dc = HostActivity.this.createDisplayContext(display);
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
            Class<?> providerClass = Class.forName("android.view.InsetsFrameProvider");
            Constructor<?> constructor = providerClass.getConstructor(Object.class, int.class, int.class);
            Object providers = Array.newInstance(providerClass, 3);
            int[] types = { WindowInsets.Type.statusBars(), WindowInsets.Type.tappableElement(), WindowInsets.Type.mandatorySystemGestures() };
            Binder owner = new Binder();
            for (int index = 0; index < types.length; index++) {
                Object provider = constructor.newInstance(owner, 0, types[index]);
                providerClass.getMethod("setInsetsSize", Insets.class).invoke(provider, Insets.of(0, height, 0, 0));
                Array.set(providers, index, provider);
            }
            WindowManager.LayoutParams.class.getField("providedInsets").set(params, providers);
        }
        void add() { manager.addView(bar, params); }
        void remove() { manager.removeViewImmediate(bar); }
        JSONObject status() throws Exception {
            return new JSONObject().put("displayId", display.getDisplayId()).put("displayValid", display.isValid())
                    .put("barExists", true).put("attached", bar.isAttachedToWindow()).put("visibility", bar.getVisibility())
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
            paint.setColor(dark ? Color.BLACK : Color.WHITE);
            paint.setTextSize(16);
            paint.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
            canvas.drawText("12:00", 12, getHeight() - 7, paint);
            canvas.drawRect(getWidth() - 46, 9, getWidth() - 42, getHeight() - 8, paint);
            canvas.drawRect(getWidth() - 39, 6, getWidth() - 35, getHeight() - 8, paint);
            canvas.drawRect(getWidth() - 32, 3, getWidth() - 28, getHeight() - 8, paint);
            paint.setStyle(Paint.Style.STROKE); paint.setStrokeWidth(2);
            canvas.drawRect(getWidth() - 22, 7, getWidth() - 8, getHeight() - 8, paint);
            paint.setStyle(Paint.Style.FILL);
            canvas.drawRect(getWidth() - 20, 9, getWidth() - 13, getHeight() - 10, paint);
            if (screen.lastApplied != null && (lastDrawnSequence != screen.lastApplied.seq || lastDrawnEventTime != screen.lastApplied.time)) {
                lastDrawnSequence = screen.lastApplied.seq;
                lastDrawnEventTime = screen.lastApplied.time;
                lastDrawnAt = android.os.SystemClock.elapsedRealtime();
                try {
                    android.util.Log.i(Commands.TAG, "BUILDPLOY_STATUSBAR_DRAW " + new JSONObject()
                            .put("displayId", screen.display.getDisplayId()).put("seq", lastDrawnSequence)
                            .put("drawnAtElapsedRealtime", lastDrawnAt).put("darkIcons", dark).put("event", screen.lastApplied.json()));
                } catch (Exception ignored) { /* Only primitive prototype metadata is serialized. */ }
            }
        }
    }
}
