package net.sentinel.live.ui.playback;

import android.os.Bundle;
import android.view.View;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.ListView;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;

import net.sentinel.live.R;
import net.sentinel.live.data.ApiClient;
import net.sentinel.live.data.Recording;

import java.util.ArrayList;
import java.util.List;

public class PlaybackActivity extends AppCompatActivity {

    private ListView listView;
    private TextView tvEmpty;
    private Button btnRefresh;
    private ArrayAdapter<Recording> adapter;
    private List<Recording> recordings = new ArrayList<>();
    private ApiClient apiClient;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_playback);

        apiClient = new ApiClient(this);

        listView = findViewById(R.id.list_recordings);
        tvEmpty = findViewById(R.id.tv_empty);
        btnRefresh = findViewById(R.id.btn_refresh);

        adapter = new ArrayAdapter<Recording>(this, android.R.layout.simple_list_item_2, recordings) {
            @Override
            public View getView(int position, View convertView, android.view.ViewGroup parent) {
                View view = super.getView(position, convertView, parent);
                Recording r = getItem(position);
                if (view instanceof android.widget.TextView) {
                    ((android.widget.TextView) view).setText(r.file + "\n" + formatSize(r.size));
                }
                return view;
            }
        };
        listView.setAdapter(adapter);

        listView.setOnItemClickListener((parent, view, position, id) -> {
            Recording r = recordings.get(position);
            Toast.makeText(this, "Download: " + r.file, Toast.LENGTH_SHORT).show();
        });

        btnRefresh.setOnClickListener(v -> loadRecordings());
        loadRecordings();
    }

    @Override
    protected void onResume() {
        super.onResume();
        loadRecordings();
    }

    private void loadRecordings() {
        apiClient.getRecordings(new ApiClient.Callback<List<Recording>>() {
            @Override
            public void onSuccess(List<Recording> result) {
                recordings.clear();
                recordings.addAll(result);
                adapter.notifyDataSetChanged();
                tvEmpty.setVisibility(recordings.isEmpty() ? View.VISIBLE : View.GONE);
            }

            @Override
            public void onError(String error) {
                Toast.makeText(PlaybackActivity.this, "Failed: " + error, Toast.LENGTH_SHORT).show();
            }
        });
    }

    private String formatSize(long bytes) {
        if (bytes < 1024) return bytes + " B";
        if (bytes < 1024 * 1024) return (bytes / 1024) + " KB";
        if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)) + " MB";
        return (bytes / (1024 * 1024 * 1024)) + " GB";
    }
}
