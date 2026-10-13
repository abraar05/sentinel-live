package net.sentinel.live.ui.devices;

import android.os.Bundle;
import android.view.Menu;
import android.view.MenuItem;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.Toast;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;

import net.sentinel.live.R;
import net.sentinel.live.data.ApiClient;
import net.sentinel.live.data.Device;

import org.json.JSONObject;

public class DeviceEditActivity extends AppCompatActivity {

    private EditText etName, etHost, etPort, etUsername, etPassword, etStoragePath;
    private CheckBox cbEnabled;
    private Button btnSave, btnDelete;
    private ApiClient apiClient;
    private String deviceId;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_device_edit);

        apiClient = new ApiClient(this);
        deviceId = getIntent().getStringExtra("device_id");

        etName = findViewById(R.id.et_name);
        etHost = findViewById(R.id.et_host);
        etPort = findViewById(R.id.et_port);
        etUsername = findViewById(R.id.et_username);
        etPassword = findViewById(R.id.et_password);
        etStoragePath = findViewById(R.id.et_storage_path);
        cbEnabled = findViewById(R.id.cb_enabled);
        btnSave = findViewById(R.id.btn_save);
        btnDelete = findViewById(R.id.btn_delete);

        if (deviceId != null && !deviceId.isEmpty()) {
            loadDevice();
            btnDelete.setVisibility(View.VISIBLE);
        } else {
            btnDelete.setVisibility(View.GONE);
            etPort.setText("34567");
            etUsername.setText("admin");
        }

        btnSave.setOnClickListener(v -> saveDevice());
        btnDelete.setOnClickListener(v -> deleteDevice());
    }

    private void loadDevice() {
        apiClient.getDevices(new ApiClient.Callback<java.util.List<Device>>() {
            @Override
            public void onSuccess(java.util.List<Device> result) {
                for (Device d : result) {
                    if (d.id.equals(deviceId)) {
                        runOnUiThread(() -> {
                            etName.setText(d.name);
                            etHost.setText(d.host);
                            etPort.setText(String.valueOf(d.port));
                            etUsername.setText(d.username);
                            etStoragePath.setText(d.storagePath);
                            cbEnabled.setChecked(d.enabled);
                        });
                        break;
                    }
                }
            }

            @Override
            public void onError(String error) {
                runOnUiThread(() -> Toast.makeText(DeviceEditActivity.this, "Failed: " + error, Toast.LENGTH_SHORT).show());
            }
        });
    }

    private void saveDevice() {
        try {
            Device device = new Device();
            device.name = etName.getText().toString().trim();
            device.host = etHost.getText().toString().trim();
            device.port = Integer.parseInt(etPort.getText().toString().trim());
            device.username = etUsername.getText().toString().trim();
            device.password = etPassword.getText().toString();
            device.storagePath = etStoragePath.getText().toString().trim();
            device.enabled = cbEnabled.isChecked();

            if (device.name.isEmpty()) {
                Toast.makeText(this, "Name is required", Toast.LENGTH_SHORT).show();
                return;
            }

            ApiClient.Callback<JSONObject> callback = new ApiClient.Callback<JSONObject>() {
                @Override
                public void onSuccess(JSONObject result) {
                    runOnUiThread(() -> {
                        Toast.makeText(DeviceEditActivity.this, "Saved", Toast.LENGTH_SHORT).show();
                        finish();
                    });
                }

                @Override
                public void onError(String error) {
                    runOnUiThread(() -> Toast.makeText(DeviceEditActivity.this, "Error: " + error, Toast.LENGTH_SHORT).show());
                }
            };

            if (deviceId != null && !deviceId.isEmpty()) {
                device.id = deviceId;
                apiClient.updateDevice(device, callback);
            } else {
                apiClient.addDevice(device, callback);
            }
        } catch (Exception e) {
            Toast.makeText(this, "Error: " + e.getMessage(), Toast.LENGTH_SHORT).show();
        }
    }

    private void deleteDevice() {
        apiClient.deleteDevice(deviceId, new ApiClient.Callback<JSONObject>() {
            @Override
            public void onSuccess(JSONObject result) {
                runOnUiThread(() -> {
                    Toast.makeText(DeviceEditActivity.this, "Deleted", Toast.LENGTH_SHORT).show();
                    finish();
                });
            }

            @Override
            public void onError(String error) {
                runOnUiThread(() -> Toast.makeText(DeviceEditActivity.this, "Error: " + error, Toast.LENGTH_SHORT).show());
            }
        });
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        getMenuInflater().inflate(R.menu.menu_device_edit, menu);
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(@NonNull MenuItem item) {
        if (item.getItemId() == R.id.action_save) {
            saveDevice();
            return true;
        }
        return super.onOptionsItemSelected(item);
    }
}
