package probe;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Insets;
import android.graphics.Rect;
import android.os.Build;
import android.os.Bundle;
import android.os.Process;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import org.json.JSONObject;

/** Isolated visual fixture: all reported bar geometry comes from actual WindowInsets. */
public final class StatusBarProbe extends Activity {
    private FrameLayout root;
    private LinearLayout content;
    private TextView metrics;
    private Button topButton, edgeButton, iconsButton, statusButton, paddingButton;
    private boolean edge = true, darkIcons, showStatus = true, padInsets = true;
    private int clicks;
    private String lastReport = "";
    private String lastText = "";
    private SharedPreferences preferences;
    private String requestId = "";
    private boolean preferenceWriteAttempted, preferenceWriteSucceeded;

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        boolean light;
        try {
            Bundle defaults = getPackageManager().getApplicationInfo(getPackageName(),
                    PackageManager.GET_META_DATA).metaData;
            light = defaults != null && defaults.getBoolean("probeLightTheme", false);
        } catch (PackageManager.NameNotFoundException failure) {
            throw new IllegalStateException("Fixture package metadata unavailable", failure);
        }
        darkIcons = light;
        preferences = getSharedPreferences("device-fixture", MODE_PRIVATE);
        readOptions(getIntent());
        int ink = light ? Color.rgb(15, 48, 57) : Color.WHITE;
        root = new FrameLayout(this);
        root.setBackgroundColor(light ? Color.rgb(216, 243, 240) : Color.rgb(23, 42, 86));

        TextView edgeMarker = new TextView(this);
        edgeMarker.setText("APP TOP EDGE — background marker, not a status bar");
        edgeMarker.setTextColor(Color.BLACK);
        edgeMarker.setTextSize(11);
        edgeMarker.setGravity(Gravity.CENTER);
        edgeMarker.setBackgroundColor(light ? Color.rgb(255, 204, 102) : Color.rgb(247, 143, 169));
        root.addView(edgeMarker, new FrameLayout.LayoutParams(-1, dp(36), Gravity.TOP));

        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        scroll.addView(content, new ScrollView.LayoutParams(-1, -2));
        root.addView(scroll, new FrameLayout.LayoutParams(-1, -1));
        topButton = button("", () -> { clicks++; labels(); report("top-button"); });
        TextView title = new TextView(this);
        title.setText("STATUS BAR PROBE — " + (light ? "LIGHT / MINT" : "DARK / NAVY") + "\n" + getPackageName());
        title.setTextColor(ink);
        title.setTextSize(19);
        content.addView(title);
        metrics = new TextView(this);
        metrics.setTextColor(ink);
        metrics.setTextSize(13);
        metrics.setTypeface(android.graphics.Typeface.MONOSPACE);
        content.addView(metrics);
        edgeButton = button("", () -> { edge = !edge; applyOptions(); });
        iconsButton = button("", () -> { darkIcons = !darkIcons; applyOptions(); });
        statusButton = button("", () -> { showStatus = !showStatus; applyOptions(); });
        paddingButton = button("", () -> { padInsets = !padInsets; applyOptions(); });
        TextView note = new TextView(this);
        note.setText("targetSdk=35. Android 15 enforces edge-to-edge: requested FIT may be ignored. "
                + "The colored stripe belongs to this app. Only actual system icons and the reported insets prove a real status bar. "
                + "Padding moves controls away from reported status/navigation insets; it never paints fake system icons.");
        note.setTextColor(ink);
        note.setTextSize(13);
        content.addView(note);
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            padding(insets);
            root.post(() -> report("onApplyWindowInsets"));
            return insets;
        });
        root.addOnLayoutChangeListener((view, l, t, r, b, ol, ot, or, ob) -> report("layout"));
        setContentView(root);
        applyOptions();
        // WindowInsetsController becomes available after the decor attaches to a display.
        root.post(this::applyOptions);
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        readOptions(intent);
        applyOptions();
    }

    private void readOptions(Intent intent) {
        edge = intent.getBooleanExtra("edge", edge);
        darkIcons = intent.getBooleanExtra("darkIcons", darkIcons);
        showStatus = intent.getBooleanExtra("showStatus", showStatus);
        padInsets = intent.getBooleanExtra("padInsets", padInsets);
        requestId = intent.getStringExtra("requestId");
        if (requestId == null) requestId = "";
        preferenceWriteAttempted = intent.hasExtra("prefsWrite");
        preferenceWriteSucceeded = false;
        if (preferenceWriteAttempted) {
            // Synchronous durability is intentional: the next restart must observe this test value.
            preferenceWriteSucceeded = preferences.edit()
                    .putString("user-value", intent.getStringExtra("prefsWrite")).commit();
        }
    }

    private Button button(String text, Runnable action) {
        Button button = new Button(this);
        button.setText(text);
        button.setAllCaps(false);
        button.setMinHeight(dp(56));
        button.setOnClickListener(view -> action.run());
        content.addView(button, new LinearLayout.LayoutParams(-1, -2));
        return button;
    }

    private void labels() {
        topButton.setText("TOP BUTTON — taps=" + clicks);
        edgeButton.setText("Requested: " + (edge ? "EDGE-TO-EDGE" : "FIT") + " — tap to switch");
        iconsButton.setText("Status icons: " + (darkIcons ? "DARK" : "LIGHT") + " — tap to switch");
        statusButton.setText("Requested status bar: " + (showStatus ? "SHOW" : "HIDE") + " — tap to switch");
        paddingButton.setText("Controls avoid bar insets: " + (padInsets ? "YES" : "NO") + " — tap to switch");
    }

    private void applyOptions() {
        getWindow().setDecorFitsSystemWindows(!edge);
        // On target 35 / Android 15 transparency is enforced; the background is app content.
        getWindow().setStatusBarColor(Color.TRANSPARENT);
        getWindow().setNavigationBarColor(Color.TRANSPARENT);
        getWindow().setStatusBarContrastEnforced(false);
        WindowInsetsController controller = getWindow().getInsetsController();
        if (controller != null) {
            controller.setSystemBarsAppearance(darkIcons ? WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS : 0,
                    WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS);
            if (showStatus) controller.show(WindowInsets.Type.statusBars());
            else controller.hide(WindowInsets.Type.statusBars());
        }
        labels();
        WindowInsets current = root.getRootWindowInsets();
        if (current != null) padding(current);
        root.requestApplyInsets();
        root.post(() -> report("options"));
    }

    private void padding(WindowInsets insets) {
        Insets bars = insets.getInsets(WindowInsets.Type.statusBars() | WindowInsets.Type.navigationBars());
        content.setPadding(dp(12) + (padInsets ? bars.left : 0), dp(12) + (padInsets ? bars.top : 0),
                dp(12) + (padInsets ? bars.right : 0), dp(12) + (padInsets ? bars.bottom : 0));
    }

    private void report(String reason) {
        WindowInsets insets = root.getRootWindowInsets();
        if (insets == null) return;
        try {
            Insets status = insets.getInsets(WindowInsets.Type.statusBars());
            Insets stableStatus = insets.getInsetsIgnoringVisibility(WindowInsets.Type.statusBars());
            Insets nav = insets.getInsets(WindowInsets.Type.navigationBars());
            Insets stableNav = insets.getInsetsIgnoringVisibility(WindowInsets.Type.navigationBars());
            Rect bounds = getWindowManager().getCurrentWindowMetrics().getBounds();
            View decor = getWindow().getDecorView();
            int[] location = new int[2]; root.getLocationOnScreen(location);
            int[] buttonLocation = new int[2]; topButton.getLocationOnScreen(buttonLocation);
            WindowInsetsController controller = getWindow().getInsetsController();
            JSONObject result = new JSONObject().put("packageName", getPackageName())
                    .put("uid", Process.myUid()).put("requestId", requestId)
                    .put("privateValue", preferences.contains("user-value")
                            ? preferences.getString("user-value", null) : JSONObject.NULL)
                    .put("preferenceWriteAttempted", preferenceWriteAttempted)
                    .put("preferenceWriteSucceeded", preferenceWriteSucceeded)
                    .put("sdkInt", Build.VERSION.SDK_INT).put("targetSdk", getApplicationInfo().targetSdkVersion)
                    .put("displayId", getDisplay().getDisplayId())
                    .put("requestedEdgeToEdge", edge).put("requestedDarkIcons", darkIcons)
                    .put("requestedShowStatus", showStatus).put("applyInsetPadding", padInsets)
                    .put("statusBars", value(status)).put("statusBarsIgnoringVisibility", value(stableStatus))
                    .put("statusBarsVisible", insets.isVisible(WindowInsets.Type.statusBars()))
                    .put("navigationBars", value(nav)).put("navigationBarsIgnoringVisibility", value(stableNav))
                    .put("navigationBarsVisible", insets.isVisible(WindowInsets.Type.navigationBars()))
                    .put("windowBounds", bounds.toShortString())
                    .put("decorWidth", decor.getWidth()).put("decorHeight", decor.getHeight())
                    .put("rootWidth", root.getWidth()).put("rootHeight", root.getHeight())
                    .put("rootScreenX", location[0]).put("rootScreenY", location[1])
                    .put("controlsPaddingTop", content.getPaddingTop()).put("controlsPaddingBottom", content.getPaddingBottom())
                    .put("systemBarsAppearance", controller == null ? -1 : controller.getSystemBarsAppearance())
                    .put("topButtonTaps", clicks)
                    .put("topButtonBounds", new JSONObject()
                            .put("left", buttonLocation[0]).put("top", buttonLocation[1])
                            .put("right", buttonLocation[0] + topButton.getWidth())
                            .put("bottom", buttonLocation[1] + topButton.getHeight()));
            String text = "displayId=" + getDisplay().getDisplayId() + " API=" + Build.VERSION.SDK_INT + " target=35\n"
                    + "status: " + status + " visible=" + insets.isVisible(WindowInsets.Type.statusBars()) + "\n"
                    + "status ignoringVisibility: " + stableStatus + "\n"
                    + "nav: " + nav + " visible=" + insets.isVisible(WindowInsets.Type.navigationBars()) + "\n"
                    + "nav ignoringVisibility: " + stableNav + "\n"
                    + "window=" + bounds.toShortString() + "\n"
                    + "decor=" + decor.getWidth() + "x" + decor.getHeight() + " root=" + root.getWidth() + "x" + root.getHeight()
                    + " @" + location[0] + "," + location[1] + "\n"
                    + "controls top/bottom padding=" + content.getPaddingTop() + "/" + content.getPaddingBottom() + "\n"
                    + "requested=" + (edge ? "EDGE" : "FIT") + " icons=" + (darkIcons ? "DARK" : "LIGHT")
                    + " status=" + (showStatus ? "SHOW" : "HIDE");
            if (!text.equals(lastText)) { lastText = text; metrics.setText(text); }
            String encoded = result.toString();
            if (!encoded.equals(lastReport) || "onApplyWindowInsets".equals(reason)) {
                lastReport = encoded;
                result.put("event", reason);
                Log.i("BuildployStatusBarProbe", "BUILDPLOY_STATUSBAR_PROBE " + result);
            }
        } catch (Exception failure) { Log.e("BuildployStatusBarProbe", "BUILDPLOY_STATUSBAR_PROBE_REPORT_FAILED"); }
    }

    private static JSONObject value(Insets insets) throws Exception {
        return new JSONObject().put("left", insets.left).put("top", insets.top).put("right", insets.right).put("bottom", insets.bottom);
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
}
