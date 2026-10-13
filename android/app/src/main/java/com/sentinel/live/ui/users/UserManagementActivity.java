package net.sentinel.live.ui.users;

import android.os.Bundle;
import android.view.View;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;

import net.sentinel.live.R;

public class UserManagementActivity extends AppCompatActivity {

    private TextView tvUsers;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_user_management);

        tvUsers = findViewById(R.id.tv_users);
        tvUsers.setText("User management available on web interface");
    }
}
