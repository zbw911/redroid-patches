package com.buildploy.statusbarprobe.host;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import java.util.Arrays;
import org.json.JSONObject;

/** Signature permission plus shared Binder sender identity fence the real SystemUI bridge. */
public final class AppearanceEvents extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        // goAsync clears the Receiver's PendingResult, which also owns the shared sender identity.
        int senderUid = getSentFromUid();
        String senderPackage = getSentFromPackage();
        PendingResult pending = goAsync();
        new Handler(Looper.getMainLooper()).post(() -> {
            JSONObject reply = new JSONObject();
            boolean ok = false;
            try {
                String[] packages = senderUid < 0 ? null : context.getPackageManager().getPackagesForUid(senderUid);
                if (!"com.android.systemui".equals(senderPackage) || packages == null
                        || !Arrays.asList(packages).contains("com.android.systemui")
                        || context.getPackageManager().checkSignatures(senderUid, android.os.Process.myUid()) != PackageManager.SIGNATURE_MATCH)
                    throw new SecurityException("SYSTEMUI_PLATFORM_SIGNATURE_REQUIRED");
                StatusBarHost host = (StatusBarHost) context.getApplicationContext();
                reply.put("ok", true).put("result", host.appearance(intent, senderUid));
                ok = true;
            } catch (Exception failure) {
                try { reply.put("ok", false).put("errorClass", failure.getClass().getSimpleName()).put("error", String.valueOf(failure.getMessage())); }
                catch (Exception ignored) { /* Only fixed prototype diagnostics are serialized. */ }
            } finally {
                Log.i(StatusBarHost.TAG, "BUILDPLOY_STATUSBAR_APPEARANCE " + reply);
                pending.setResultCode(ok ? Activity.RESULT_OK : Activity.RESULT_CANCELED);
                pending.setResultData(reply.toString());
                pending.finish();
            }
        });
    }
}
