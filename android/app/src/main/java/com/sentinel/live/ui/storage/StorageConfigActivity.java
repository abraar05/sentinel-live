package net.sentinel.live.ui.storage;

import android.os.Bundle;
import android.widget.Button;
import android.widget.EditText;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;

import net.sentinel.live.R;
import net.sentinel.live.data.ApiClient;

import org.json.JSONObject;

public class StorageConfigActivity extends AppCompatActivity {

    private EditText etBaseDir;
    private Button btnSave;
    private ApiClient apiClient;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_storage_config);

        apiClient = new ApiClient(this);

        etBaseDir = findViewById(R.id.et_base_dir);
        btnSave = findViewById(R.id.btn_save);

        btnSave.setOnClickListener(v -> saveStorageConfig());
    }

    private void saveStorageConfig() {
        String baseDir = etBaseDir.getText().toString().trim();
        apiClient.setStorageBaseDir(baseDir, new ApiClient.Callback<JSONObject>() {
            @Override
            public void onSuccess(JSONObject result) {
                runOnUiThread(() -> {
                    Toast.makeText(StorageConfigActivity.this, "Storage configured: " + result.optString("recordings_dir", ""), Toast.LENGTH_LONG).show();
                    finish();
                });
            }

            @Override
            public void onError(String error) {
                runOnUiThread(() -> Toast.makeText(StorageConfigActivity.this, "Error: " + error, Toast.LENGTH_SHORT).show());
            }
        });
    }
}
