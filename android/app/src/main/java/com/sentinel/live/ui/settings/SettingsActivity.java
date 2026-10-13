package net.sentinel.live.ui.settings;

import android.content.Intent;
import android.os.Bundle;
import android.view.View;
import android.widget.Button;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatActivity;

import net.sentinel.live.R;
import net.sentinel.live.data.ApiClient;
import net.sentinel.live.ui.auth.LoginActivity;
import net.sentinel.live.ui.users.UserManagementActivity;

public class SettingsActivity extends AppCompatActivity {

    private ApiClient apiClient;
    private TextView tvServerInfo;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_settings);

        apiClient = new ApiClient(this);

        tvServerInfo = findViewById(R.id.tv_server_info);
        tvServerInfo.setText("Server: " + apiClient.getBaseUrl());

        findViewById(R.id.btn_server).setOnClickListener(v -> {
            startActivity(new Intent(this, MainActivity.class));
        });

        findViewById(R.id.btn_users).setOnClickListener(v -> {
            startActivity(new Intent(this, UserManagementActivity.class));
        });

        findViewById(R.id.btn_logout).setOnClickListener(v -> {
            apiClient.clearAuth();
            Intent intent = new Intent(this, LoginActivity.class);
            intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
            startActivity(intent);
            finish();
        });
    }
}
