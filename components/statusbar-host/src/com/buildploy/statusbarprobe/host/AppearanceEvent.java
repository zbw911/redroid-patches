package com.buildploy.statusbarprobe.host;

import android.content.Intent;
import org.json.JSONArray;
import org.json.JSONObject;

/** Raw SystemUI event plus the deliberately limited uniform-region rendering decision. */
final class AppearanceEvent {
    static final String ACTION = "com.buildploy.statusbarprobe.APPEARANCE";
    static final String SOURCE = "SystemUI.CommandQueue.onSystemBarAttributesChanged";
    final int displayId, appearance, senderUid;
    final long seq, time, receivedAt;
    final String packageName, source;
    final int[] regionAppearances, regionBounds;
    final boolean supported, darkIcons;

    AppearanceEvent(Intent intent, int senderUid) {
        if (!ACTION.equals(intent.getAction())) throw new IllegalArgumentException("INVALID_EVENT_ACTION");
        displayId = intent.getIntExtra("displayId", -1);
        appearance = intent.getIntExtra("appearance", 0);
        seq = intent.getLongExtra("seq", -1);
        time = intent.getLongExtra("eventTimeElapsedRealtime", -1);
        receivedAt = android.os.SystemClock.elapsedRealtime();
        packageName = intent.getStringExtra("packageName");
        source = intent.getStringExtra("source");
        this.senderUid = senderUid;
        int[] appearances = intent.getIntArrayExtra("regionAppearances");
        int[] bounds = intent.getIntArrayExtra("regionBounds");
        if (displayId <= 0 || !intent.hasExtra("appearance") || seq <= 0 || time < 0 || time > receivedAt
                || packageName == null || packageName.length() > 255 || !SOURCE.equals(source)
                || appearances == null || bounds == null || appearances.length > 64 || bounds.length != appearances.length * 4)
            throw new IllegalArgumentException("INVALID_APPEARANCE_EVENT");
        regionAppearances = appearances.clone();
        regionBounds = bounds.clone();
        for (int index = 0; index < appearances.length; index++) {
            if (bounds[index * 4] >= bounds[index * 4 + 2] || bounds[index * 4 + 1] >= bounds[index * 4 + 3])
                throw new IllegalArgumentException("INVALID_APPEARANCE_REGION");
        }
        int first = appearances.length == 0 ? appearance : appearances[0];
        darkIcons = (first & 8) != 0;
        boolean uniform = true;
        for (int item : appearances) if (((item & 8) != 0) != darkIcons) uniform = false;
        supported = uniform;
    }

    JSONObject json() throws Exception {
        JSONArray regions = new JSONArray();
        for (int index = 0; index < regionAppearances.length; index++) {
            regions.put(new JSONObject().put("appearance", regionAppearances[index])
                    .put("bounds", new JSONArray().put(regionBounds[index * 4]).put(regionBounds[index * 4 + 1])
                            .put(regionBounds[index * 4 + 2]).put(regionBounds[index * 4 + 3])));
        }
        return new JSONObject().put("displayId", displayId).put("appearance", appearance).put("seq", seq)
                .put("eventTimeElapsedRealtime", time).put("receivedAtElapsedRealtime", receivedAt)
                .put("packageName", packageName).put("source", source).put("senderUid", senderUid)
                .put("senderPackage", "com.android.systemui").put("regions", regions)
                .put("uniformLightStatusBarRegions", supported).put("darkIcons", darkIcons);
    }
}
