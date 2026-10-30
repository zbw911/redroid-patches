package com.buildploy.statusbarprototype;

import android.app.Application;
import android.app.BroadcastOptions;
import android.content.ComponentName;
import android.content.Intent;
import android.graphics.Rect;
import android.os.Binder;
import android.os.SystemClock;
import android.util.Log;
import java.lang.reflect.Array;

/** Forward events without replacing CommandQueue or its original callback processing. */
public final class AppearanceBridge {
    public static void dispatch(int displayId, int appearance, Object regions,
            boolean navbarColorManagedByIme, int behavior, int requestedVisibleTypes,
            String packageName, Object letterboxDetails) {
        if (displayId <= 0) return;
        long identity = Binder.clearCallingIdentity();
        try {
            Application app = (Application) Class.forName("android.app.ActivityThread")
                    .getMethod("currentApplication").invoke(null);
            if (app == null) return;
            int count = regions == null ? 0 : Array.getLength(regions);
            int[] appearances = new int[count];
            int[] bounds = new int[count * 4];
            for (int i = 0; i < count; i++) {
                Object region = Array.get(regions, i);
                appearances[i] = (Integer) region.getClass().getMethod("getAppearance").invoke(region);
                Rect rect = (Rect) region.getClass().getMethod("getBounds").invoke(region);
                bounds[i * 4] = rect.left;
                bounds[i * 4 + 1] = rect.top;
                bounds[i * 4 + 2] = rect.right;
                bounds[i * 4 + 3] = rect.bottom;
            }
            // Boot-monotonic ordering survives a SystemUI process restart without retained state.
            long sequence = SystemClock.elapsedRealtimeNanos();
            Intent intent = new Intent("com.buildploy.statusbarprobe.APPEARANCE")
                    .setComponent(new ComponentName("com.buildploy.statusbarprobe.host",
                            "com.buildploy.statusbarprobe.host.AppearanceEvents"))
                    .putExtra("displayId", displayId)
                    .putExtra("appearance", appearance)
                    .putExtra("seq", sequence)
                    .putExtra("eventTimeElapsedRealtime", SystemClock.elapsedRealtime())
                    .putExtra("packageName", packageName)
                    .putExtra("source", "SystemUI.CommandQueue.onSystemBarAttributesChanged")
                    .putExtra("regionAppearances", appearances)
                    .putExtra("regionBounds", bounds);
            app.sendBroadcast(intent, "android.permission.STATUS_BAR_SERVICE",
                    BroadcastOptions.makeBasic().setShareIdentityEnabled(true).toBundle());
        } catch (Throwable failure) {
            // Observer failures must never prevent the original SystemUI callback.
            Log.w("BuildployAppearanceBridge", "bridge-error:" + failure.getClass().getSimpleName());
        } finally {
            Binder.restoreCallingIdentity(identity);
        }
    }
}
