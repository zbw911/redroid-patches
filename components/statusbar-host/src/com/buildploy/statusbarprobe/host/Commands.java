package com.buildploy.statusbarprobe.host;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import org.json.JSONObject;

/** Signature-permission-protected experiment controls; appearance has a separate SystemUI-only path. */
public final class Commands extends BroadcastReceiver {
    static final String TAG = "BuildployStatusBarHost";
    @Override public void onReceive(Context context, Intent intent) {
        PendingResult pending = goAsync();
        new Handler(Looper.getMainLooper()).post(() -> {
            JSONObject reply = new JSONObject();
            boolean ok = false;
            try {
                String action = intent.getStringExtra("action");
                if (action == null) throw new IllegalArgumentException("ACTION_REQUIRED");
                if (intent.hasExtra("appearance") || intent.hasExtra("darkIcons")) throw new IllegalArgumentException("MANUAL_APPEARANCE_DISABLED");
                HostActivity host = HostActivity.current;
                if (host == null || host.isDestroyed()) throw new IllegalStateException("HOST_ACTIVITY_NOT_RUNNING");
                JSONObject result = host.command(action, intent.getIntExtra("displayId", -1), intent.getIntExtra("height", 28));
                reply.put("ok", true).put("action", action).put("result", result);
                ok = true;
            } catch (Throwable failure) {
                try {
                    Throwable cause = failure;
                    while (cause.getCause() != null && cause.getCause() != cause) cause = cause.getCause();
                    reply.put("ok", false).put("errorClass", cause.getClass().getSimpleName())
                            .put("error", String.valueOf(cause.getMessage()));
                } catch (Exception ignored) { /* Primitive diagnostics have no application data. */ }
            } finally {
                String encoded = reply.toString();
                Log.i(TAG, "BUILDPLOY_STATUSBAR_HOST " + encoded);
                pending.setResultCode(ok ? Activity.RESULT_OK : Activity.RESULT_CANCELED);
                pending.setResultData(encoded);
                pending.finish();
            }
        });
    }
}
