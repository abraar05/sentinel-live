package net.sentinel.live.ui.motion;

import android.os.Bundle;
import android.view.View;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.ListView;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;

import net.sentinel.live.R;
import net.sentinel.live.data.ApiClient;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

public class MotionEventActivity extends AppCompatActivity {

    private ListView listView;
    private TextView tvEmpty;
    private Button btnRefresh;
    private ApiClient apiClient;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_motion_events);

        apiClient = new ApiClient(this);

        listView = findViewById(R.id.list_events);
        tvEmpty = findViewById(R.id.tv_empty);
        btnRefresh = findViewById(R.id.btn_refresh);

        btnRefresh.setOnClickListener(v -> loadEvents());
        loadEvents();
    }

    @Override
    protected void onResume() {
        super.onResume();
        loadEvents();
    }

    private void loadEvents() {
        apiClient.getHealth(new ApiClient.Callback<JSONObject>() {
            @Override
            public void onSuccess(JSONObject result) {
                JSONArray events = result.optJSONArray("events");
                List<String> eventList = new ArrayList<>();
                if (events != null) {
                    for (int i = 0; i < events.length(); i++) {
                        JSONObject e = events.optJSONObject(i);
                        if (e != null) {
                            eventList.add(e.optString("deviceName", e.optString("deviceId", "?")) +
                                " - " + e.optDouble("score", 0));
                        }
                    }
                }
                final List<String> finalEvents = eventList;
                runOnUiThread(() -> {
                    ArrayAdapter<String> adapter = new ArrayAdapter<>(
                        MotionEventActivity.this,
                        android.R.layout.simple_list_item_1,
                        finalEvents
                    );
                    listView.setAdapter(adapter);
                    tvEmpty.setVisibility(finalEvents.isEmpty() ? View.VISIBLE : View.GONE);
                });
            }

            @Override
            public void onError(String error) {
                runOnUiThread(() -> Toast.makeText(MotionEventActivity.this, "Failed: " + error, Toast.LENGTH_SHORT).show());
            }
        });
    }
}
