package net.sentinel.live.ui.live;

import android.app.Dialog;
import android.content.Context;
import android.view.View;
import android.widget.Button;
import android.widget.GridLayout;

import net.sentinel.live.R;
import net.sentinel.live.data.ApiClient;
import net.sentinel.live.data.Device;

public class PtzControlDialog extends Dialog {

    private final Device device;
    private final ApiClient apiClient;

    public PtzControlDialog(Context context, Device device, ApiClient apiClient) {
        super(context);
        this.device = device;
        this.apiClient = apiClient;
        setContentView(R.layout.dialog_ptz);
        setTitle("PTZ Control - " + device.name);

        GridLayout grid = findViewById(R.id.ptz_grid);
        if (grid != null) {
            setupPtzButtons(grid);
        }
    }

    private void setupPtzButtons(GridLayout grid) {
        String[] commands = {
            "DirectionUp", "DirectionDown", "DirectionLeft", "DirectionRight",
            "DirectionLeftUp", "DirectionLeftDown", "DirectionRightUp", "DirectionRightDown",
            "ZoomTile", "ZoomWide", "FocusNear", "FocusFar"
        };

        for (String cmd : commands) {
            Button btn = new Button(getContext());
            btn.setText(getPtzLabel(cmd));
            btn.setOnClickListener(v -> sendPtzCommand(cmd));
            GridLayout.LayoutParams params = new GridLayout.LayoutParams();
            params.width = GridLayout.LayoutParams.WRAP_CONTENT;
            params.height = GridLayout.LayoutParams.WRAP_CONTENT;
            params.setMargins(4, 4, 4, 4);
            grid.addView(btn, params);
        }

        Button closeBtn = findViewById(R.id.btn_ptz_close);
        if (closeBtn != null) {
            closeBtn.setOnClickListener(v -> dismiss());
        }
    }

    private String getPtzLabel(String cmd) {
        switch (cmd) {
            case "DirectionUp": return "Up";
            case "DirectionDown": return "Down";
            case "DirectionLeft": return "Left";
            case "DirectionRight": return "Right";
            case "DirectionLeftUp": return "Up-Left";
            case "DirectionLeftDown": return "Down-Left";
            case "DirectionRightUp": return "Up-Right";
            case "DirectionRightDown": return "Down-Right";
            case "ZoomTile": return "Zoom+";
            case "ZoomWide": return "Zoom-";
            case "FocusNear": return "Focus+";
            case "FocusFar": return "Focus-";
            default: return cmd;
        }
    }

    private void sendPtzCommand(String command) {
        apiClient.ptzControl(device.id, command, new ApiClient.Callback<org.json.JSONObject>() {
            @Override
            public void onSuccess(org.json.JSONObject result) {}

            @Override
            public void onError(String error) {}
        });
    }
}
