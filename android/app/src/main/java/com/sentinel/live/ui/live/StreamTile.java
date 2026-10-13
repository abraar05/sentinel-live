package net.sentinel.live.ui.live;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Handler;
import android.os.Looper;
import android.view.LayoutInflater;
import android.view.View;
import android.widget.Button;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;

import net.sentinel.live.R;
import net.sentinel.live.data.ApiClient;
import net.sentinel.live.data.Device;

import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class StreamTile {

    private final View view;
    private final ImageView imageView;
    private final TextView tvName;
    private final TextView tvStatus;
    private final Button btnRecord;
    private final Button btnMotion;
    private final Button btnPtz;
    private final Device device;
    private final ApiClient apiClient;
    private final ExecutorService executor;
    private final Handler mainHandler;
    private boolean released = false;
    private boolean recording = false;
    private boolean motion = false;

    public StreamTile(View parent, Device device, ApiClient apiClient) {
        this.device = device;
        this.apiClient = apiClient;
        this.executor = Executors.newSingleThreadExecutor();
        this.mainHandler = new Handler(Looper.getMainLooper());

        view = LayoutInflater.from(parent.getContext()).inflate(R.layout.tile_stream, null);
        imageView = view.findViewById(R.id.tile_image);
        tvName = view.findViewById(R.id.tile_name);
        tvStatus = view.findViewById(R.id.tile_status);
        btnRecord = view.findViewById(R.id.btn_record);
        btnMotion = view.findViewById(R.id.btn_motion);
        btnPtz = view.findViewById(R.id.btn_ptz);

        tvName.setText(device.name);
        updateStatus();

        btnRecord.setOnClickListener(v -> toggleRecord());
        btnMotion.setOnClickListener(v -> toggleMotion());
        btnPtz.setOnClickListener(v -> showPtzControls());

        startSnapshotPolling();
    }

    public View getView() {
        return view;
    }

    private void updateStatus() {
        String status = device.state != null ? device.state : "idle";
        if (device.recording) status += " | REC";
        if (device.motion) status += " | MOTION";
        tvStatus.setText(status);
    }

    private void startSnapshotPolling() {
        executor.execute(() -> {
            while (!released) {
                try {
                    URL url = new URL(apiClient.getSnapshotUrl(device.id));
                    HttpURLConnection conn = (HttpURLConnection) url.openConnection();
                    conn.setConnectTimeout(5000);
                    conn.setReadTimeout(10000);
                    String token = apiClient.getToken();
                    if (token != null && !token.isEmpty()) {
                        conn.setRequestProperty("Authorization", "Bearer " + token);
                    }
                    if (conn.getResponseCode() == 200) {
                        try (InputStream is = conn.getInputStream()) {
                            final Bitmap bitmap = BitmapFactory.decodeStream(is);
                            if (bitmap != null) {
                                mainHandler.post(() -> {
                                    if (!released) imageView.setImageBitmap(bitmap);
                                });
                            }
                        }
                    }
                    conn.disconnect();
                } catch (Exception e) {
                    // ignore
                }
                try {
                    Thread.sleep(2000);
                } catch (InterruptedException e) {
                    break;
                }
            }
        });
    }

    private void toggleRecord() {
        recording = !recording;
        apiClient.setRecording(device.id, recording, new ApiClient.Callback<org.json.JSONObject>() {
            @Override
            public void onSuccess(org.json.JSONObject result) {
                device.recording = recording;
                mainHandler.post(() -> {
                    btnRecord.setText(recording ? "Stop" : "REC");
                    updateStatus();
                });
            }

            @Override
            public void onError(String error) {
                recording = !recording;
                mainHandler.post(() -> {
                    btnRecord.setText("REC");
                    updateStatus();
                });
            }
        });
    }

    private void toggleMotion() {
        motion = !motion;
        apiClient.setMotion(device.id, motion, new ApiClient.Callback<org.json.JSONObject>() {
            @Override
            public void onSuccess(org.json.JSONObject result) {
                device.motion = motion;
                mainHandler.post(() -> {
                    btnMotion.setText(motion ? "On" : "Motion");
                    updateStatus();
                });
            }

            @Override
            public void onError(String error) {
                motion = !motion;
                mainHandler.post(() -> {
                    btnMotion.setText("Motion");
                    updateStatus();
                });
            }
        });
    }

    private void showPtzControls() {
        PtzControlDialog dialog = new PtzControlDialog(view.getContext(), device, apiClient);
        dialog.show();
    }

    public void release() {
        released = true;
        executor.shutdownNow();
    }
}
