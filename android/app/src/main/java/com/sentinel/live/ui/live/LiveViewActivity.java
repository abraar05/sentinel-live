package net.sentinel.live.ui.live;

import android.content.Intent;
import android.os.Bundle;
import android.view.Menu;
import android.view.MenuItem;
import android.view.View;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.GridLayout;
import android.widget.ImageView;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;

import net.sentinel.live.R;
import net.sentinel.live.data.ApiClient;
import net.sentinel.live.data.Device;
import net.sentinel.live.ui.devices.DeviceListActivity;
import net.sentinel.live.ui.motion.MotionEventActivity;
import net.sentinel.live.ui.playback.PlaybackActivity;
import net.sentinel.live.ui.settings.SettingsActivity;
import net.sentinel.live.ui.storage.StorageConfigActivity;

import java.util.ArrayList;
import java.util.List;

public class LiveViewActivity extends AppCompatActivity {

    private ApiClient apiClient;
    private GridLayout gridLayout;
    private TextView tvEmpty;
    private Spinner spinnerLayout;
    private List<Device> devices = new ArrayList<>();
    private List<StreamTile> tiles = new ArrayList<>();
    private int currentLayout = 4;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_live_view);

        apiClient = new ApiClient(this);

        gridLayout = findViewById(R.id.grid_layout);
        tvEmpty = findViewById(R.id.tv_empty);
        spinnerLayout = findViewById(R.id.spinner_layout);

        setupLayoutSpinner();
        loadDevices();

        findViewById(R.id.btn_refresh).setOnClickListener(v -> loadDevices());
        findViewById(R.id.btn_add_device).setOnClickListener(v -> {
            startActivity(new Intent(this, DeviceListActivity.class));
        });
    }

    @Override
    protected void onResume() {
        super.onResume();
        loadDevices();
    }

    private void setupLayoutSpinner() {
        String[] layouts = {"1", "2", "4", "6", "9", "16"};
        ArrayAdapter<String> adapter = new ArrayAdapter<>(this, android.R.layout.simple_spinner_item, layouts);
        adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        spinnerLayout.setAdapter(adapter);
        spinnerLayout.setSelection(2);

        spinnerLayout.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            @Override
            public void onItemSelected(AdapterView<?> parent, View view, int position, long id) {
                currentLayout = Integer.parseInt(layouts[position]);
                renderGrid();
            }

            @Override
            public void onNothingSelected(AdapterView<?> parent) {}
        });
    }

    private void loadDevices() {
        apiClient.getDevices(new ApiClient.Callback<List<Device>>() {
            @Override
            public void onSuccess(List<Device> result) {
                devices = result;
                renderGrid();
            }

            @Override
            public void onError(String error) {
                Toast.makeText(LiveViewActivity.this, "Failed to load devices: " + error, Toast.LENGTH_SHORT).show();
            }
        });
    }

    private void renderGrid() {
        gridLayout.removeAllViews();
        tiles.clear();

        tvEmpty.setVisibility(devices.isEmpty() ? View.VISIBLE : View.GONE);
        gridLayout.setVisibility(devices.isEmpty() ? View.GONE : View.VISIBLE);

        int cols = getColumns(currentLayout);
        gridLayout.setColumnCount(cols);
        gridLayout.setRowCount(getRows(currentLayout));

        int count = Math.min(devices.size(), currentLayout);
        for (int i = 0; i < count; i++) {
            Device device = devices.get(i);
            StreamTile tile = new StreamTile(this, device, apiClient);
            tiles.add(tile);

            GridLayout.LayoutParams params = new GridLayout.LayoutParams();
            params.width = 0;
            params.height = 0;
            params.columnSpec = GridLayout.spec(i % cols, 1f);
            params.rowSpec = GridLayout.spec(i / cols, 1f);
            params.setMargins(2, 2, 2, 2);

            gridLayout.addView(tile.getView(), params);
        }
    }

    private int getColumns(int layout) {
        switch (layout) {
            case 1: return 1;
            case 2: return 2;
            case 4: return 2;
            case 6: return 3;
            case 9: return 3;
            case 16: return 4;
            default: return 2;
        }
    }

    private int getRows(int layout) {
        switch (layout) {
            case 1: return 1;
            case 2: return 1;
            case 4: return 2;
            case 6: return 2;
            case 9: return 3;
            case 16: return 4;
            default: return 2;
        }
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        getMenuInflater().inflate(R.menu.menu_live, menu);
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(@NonNull MenuItem item) {
        int id = item.getItemId();
        if (id == R.id.action_devices) {
            startActivity(new Intent(this, DeviceListActivity.class));
            return true;
        } else if (id == R.id.action_playback) {
            startActivity(new Intent(this, PlaybackActivity.class));
            return true;
        } else if (id == R.id.action_storage) {
            startActivity(new Intent(this, StorageConfigActivity.class));
            return true;
        } else if (id == R.id.action_motion_events) {
            startActivity(new Intent(this, MotionEventActivity.class));
            return true;
        } else if (id == R.id.action_settings) {
            startActivity(new Intent(this, SettingsActivity.class));
            return true;
        } else if (id == R.id.action_logout) {
            apiClient.clearAuth();
            finish();
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        for (StreamTile tile : tiles) {
            tile.release();
        }
    }
}
