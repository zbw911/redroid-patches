package probe;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.net.LocalSocket;
import android.net.LocalSocketAddress;
import android.os.Handler;
import android.os.Looper;
import android.view.Display;
import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.lang.reflect.Method;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import org.json.JSONArray;
import org.json.JSONObject;

/** Task-owned display creation and capture only; windows belong to a normal Android APK. */
public final class StatusBarProbe {
    final Context system;
    final Handler handler = new Handler(Looper.getMainLooper());
    final Map<String, Screen> screens = new LinkedHashMap<>();
    StatusBarProbe() throws Exception {
        Object thread = Class.forName("android.app.ActivityThread").getMethod("systemMain").invoke(null);
        system = (Context) thread.getClass().getMethod("getSystemContext").invoke(thread);
    }
    public static void main(String[] args) throws Exception {
        if (args.length == 1 && "host-rpc".equals(args[0])) {
            try {
                String line = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8)).readLine();
                System.out.println(host(new JSONObject(line)));
            } catch (Exception denied) {
                System.out.println(new JSONObject().put("ok", false).put("transportError", denied.getClass().getSimpleName()));
            }
            return;
        }
        if (android.os.Process.myUid() != 0) throw new SecurityException("isolated root device required");
        Looper.prepareMainLooper();
        StatusBarProbe probe = new StatusBarProbe();
        new Thread(() -> {
            try (BufferedReader reader = new BufferedReader(new InputStreamReader(System.in))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    final String request = line;
                    probe.handler.post(() -> probe.request(request));
                }
            } catch (Exception e) { e.printStackTrace(System.err); }
            probe.handler.post(() -> { for (Screen s : probe.screens.values()) s.close(); System.exit(0); });
        }, "statusbar-probe-input").start();
        Looper.loop();
    }
    void request(String line) {
        JSONObject result = new JSONObject();
        try {
            JSONObject command = new JSONObject(line);
            result.put("id", command.opt("id")).put("result", execute(command)).put("ok", true);
        } catch (Throwable e) {
            e.printStackTrace(System.err);
            try { result.put("ok", false).put("error", e.toString()).put("cause", String.valueOf(e.getCause())); }
            catch (Exception ignored) {}
        }
        System.out.println(result); System.out.flush();
    }
    JSONObject execute(JSONObject c) throws Exception {
        String action = c.getString("action");
        if (action.equals("host")) return host(c.getJSONObject("request"));
        if (action.equals("display-info")) return identity(display(c.getInt("displayId")));
        if (action.equals("snapshot") && c.has("displayId")) return snapshot(display(c.getInt("displayId")));
        if (action.equals("status") && !c.has("key")) {
            JSONArray values = new JSONArray();
            for (Screen screen : screens.values()) values.put(screen.identity());
            return new JSONObject().put("screens", values);
        }
        String key = c.getString("key");
        if (action.equals("create")) return create(key, c);
        Screen s = screens.get(key);
        if (s == null) throw new IllegalArgumentException("unknown screen");
        if (action.equals("status")) return s.identity();
        if (action.equals("snapshot")) return snapshot(s);
        if (action.equals("resize")) {
            int width = c.getInt("width"), height = c.getInt("height"), dpi = c.getInt("dpi");
            if (width < 240 || width > 1600 || height < 240 || height > 3200 || dpi < 120 || dpi > 640) throw new IllegalArgumentException("geometry");
            ImageReader next = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 3);
            next.setOnImageAvailableListener(r -> { try (Image image = r.acquireLatestImage()) {} }, handler);
            s.display.resize(width, height, dpi); s.display.setSurface(next.getSurface());
            s.reader.close(); s.reader = next;
            return s.identity();
        }
        if (action.equals("release")) { s.close(); screens.remove(key); return new JSONObject().put("released", true); }
        throw new IllegalArgumentException("unsupported action");
    }
    JSONObject create(String key, JSONObject command) throws Exception {
        if (!key.matches("[a-zA-Z0-9_-]+") || screens.containsKey(key)) throw new IllegalArgumentException("invalid key");
        String runtime = command.getString("runtimeId"), generation = command.getString("generation");
        if (!runtime.matches("[A-Za-z0-9_-]{1,128}") || !generation.matches("[A-Za-z0-9_-]{1,128}")) throw new IllegalArgumentException("invalid identity");
        ImageReader reader = ImageReader.newInstance(480, 800, PixelFormat.RGBA_8888, 3);
        reader.setOnImageAvailableListener(r -> { try (Image image = r.acquireLatestImage()) {} }, handler);
        int flags = DisplayManager.VIRTUAL_DISPLAY_FLAG_PUBLIC | DisplayManager.VIRTUAL_DISPLAY_FLAG_PRESENTATION | DisplayManager.VIRTUAL_DISPLAY_FLAG_OWN_CONTENT_ONLY;
        for (String name : new String[]{"VIRTUAL_DISPLAY_FLAG_SUPPORTS_TOUCH", "VIRTUAL_DISPLAY_FLAG_TRUSTED", "VIRTUAL_DISPLAY_FLAG_OWN_DISPLAY_GROUP", "VIRTUAL_DISPLAY_FLAG_ALWAYS_UNLOCKED", "VIRTUAL_DISPLAY_FLAG_DESTROY_CONTENT_ON_REMOVAL"}) flags |= DisplayManager.class.getField(name).getInt(null);
        VirtualDisplay display = system.getSystemService(DisplayManager.class).createVirtualDisplay("buildploy-preview-" + runtime + "-" + generation + "-" + UUID.randomUUID(), 480, 800, 160, reader.getSurface(), flags);
        if (display == null) { reader.close(); throw new IllegalStateException("no virtual display"); }
        Screen screen = new Screen(key, display, reader);
        screens.put(key, screen);
        return screen.identity();
    }
    Display display(int id) {
        Display display = system.getSystemService(DisplayManager.class).getDisplay(id);
        if (id <= 0 || display == null || !display.isValid()) throw new IllegalArgumentException("display unavailable");
        return display;
    }
    static JSONObject host(JSONObject request) throws Exception {
        try (LocalSocket socket = new LocalSocket()) {
            socket.connect(new LocalSocketAddress("buildploy-statusbar-host-v1", LocalSocketAddress.Namespace.ABSTRACT));
            socket.setSoTimeout(8000);
            socket.getOutputStream().write((request.toString() + "\n").getBytes(StandardCharsets.US_ASCII));
            String line = new BufferedReader(new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8)).readLine();
            if (line == null) throw new IllegalStateException("HOST_PEER_CLOSED");
            return new JSONObject(line);
        }
    }
    static JSONObject identity(Display display) throws Exception {
        android.util.DisplayMetrics metrics = new android.util.DisplayMetrics(); display.getRealMetrics(metrics);
        return new JSONObject().put("displayId", display.getDisplayId()).put("valid", display.isValid())
                .put("displayUniqueId", Display.class.getMethod("getUniqueId").invoke(display))
                .put("displayName", display.getName()).put("ownerUid", Display.class.getMethod("getOwnerUid").invoke(display))
                .put("width", metrics.widthPixels).put("height", metrics.heightPixels).put("dpi", metrics.densityDpi);
    }
    JSONObject snapshot(Screen s) throws Exception {
        return snapshot(s.display.getDisplay());
    }
    JSONObject snapshot(Display display) throws Exception {
        Class<?> capture = Class.forName("android.window.ScreenCapture");
        Object listener = capture.getMethod("createSyncCaptureListener").invoke(null);
        Object wm = Class.forName("android.view.WindowManagerGlobal").getMethod("getWindowManagerService").invoke(null);
        for (Method method : wm.getClass().getMethods()) if (method.getName().equals("captureDisplay")) { method.invoke(wm, display.getDisplayId(), null, listener); break; }
        Object buffer = listener.getClass().getMethod("getBuffer").invoke(listener);
        if (buffer == null) throw new IllegalStateException("capture unavailable");
        Bitmap bitmap = (Bitmap) buffer.getClass().getMethod("asBitmap").invoke(buffer);
        try (ByteArrayOutputStream bytes = new ByteArrayOutputStream()) {
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, bytes);
            return identity(display).put("pngBase64", android.util.Base64.encodeToString(bytes.toByteArray(), android.util.Base64.NO_WRAP));
        } finally { bitmap.recycle(); }
    }
    static final class Screen {
        final String key; final VirtualDisplay display; ImageReader reader;
        Screen(String key, VirtualDisplay display, ImageReader reader) { this.key=key; this.display=display; this.reader=reader; }
        int id() { return display.getDisplay().getDisplayId(); }
        JSONObject identity() throws Exception { return StatusBarProbe.identity(display.getDisplay()).put("key", key).put("decor", false); }
        void close() { display.release(); reader.close(); }
    }
}
