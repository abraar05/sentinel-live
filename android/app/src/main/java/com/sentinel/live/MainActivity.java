package net.sentinel.live;

import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

import net.sentinel.live.data.ApiClient;

import org.json.JSONObject;

public class MainActivity extends AppCompatActivity {

    private static final int PERMISSION_REQUEST_CODE = 100;

    private EditText etBaseUrl;
    private Button btnConnect;
    private ProgressBar progressBar;
    private TextView tvStatus;

    private ApiClient apiClient;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        apiClient = new ApiClient(this);

        etBaseUrl = findViewById(R.id.et_base_url);
        btnConnect = findViewById(R.id.btn_connect);
        progressBar = findViewById(R.id.progress_bar);
        tvStatus = findViewById(R.id.tv_status);

        etBaseUrl.setText(apiClient.getBaseUrl());

        btnConnect.setOnClickListener(v -> {
            String url = etBaseUrl.getText().toString().trim();
            if (url.isEmpty()) {
                Toast.makeText(this, "Please enter server URL", Toast.LENGTH_SHORT).show();
                return;
            }
            apiClient.setBaseUrl(url);
            checkAuth();
        });

        requestPermissions();
    }

    private void requestPermissions() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            String[] permissions = {
                Manifest.permission.INTERNET,
                Manifest.permission.ACCESS_NETWORK_STATE,
                Manifest.permission.WRITE_EXTERNAL_STORAGE
            };
            boolean needRequest = false;
            for (String p : permissions) {
                if (ContextCompat.checkSelfPermission(this, p) != PackageManager.PERMISSION_GRANTED) {
                    needRequest = true;
                    break;
                }
            }
            if (needRequest) {
                ActivityCompat.requestPermissions(this, permissions, PERMISSION_REQUEST_CODE);
            }
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, @NonNull String[] permissions, @NonNull int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
    }

    private void checkAuth() {
        progressBar.setVisibility(View.VISIBLE);
        tvStatus.setText("Checking connection...");

        apiClient.getDevices(new ApiClient.Callback<java.util.List<net.sentinel.live.data.Device>>() {
            @Override
            public void onSuccess(java.util.List<net.sentinel.live.data.Device> result) {
                progressBar.setVisibility(View.GONE);
                tvStatus.setText("Connected! Found " + result.size() + " devices.");
                startActivity(new Intent(MainActivity.this, net.sentinel.live.ui.live.LiveViewActivity.class));
            }

            @Override
            public void onError(String error) {
                progressBar.setVisibility(View.GONE);
                tvStatus.setText("Error: " + error);
                Toast.makeText(MainActivity.this, "Failed to connect: " + error, Toast.LENGTH_LONG).show();
            }
        });
    }
}
