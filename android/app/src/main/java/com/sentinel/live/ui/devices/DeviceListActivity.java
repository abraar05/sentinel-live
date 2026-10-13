package net.sentinel.live.ui.devices;

import android.content.Intent;
import android.os.Bundle;
import android.view.Menu;
import android.view.MenuItem;
import android.view.View;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.ListView;
import android.widget.TextView;
import android.widget.Toast;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;

import net.sentinel.live.R;
import net.sentinel.live.data.ApiClient;
import net.sentinel.live.data.Device;

import java.util.ArrayList;
import java.util.List;

public class DeviceListActivity extends AppCompatActivity {

    private ListView listView;
    private TextView tvEmpty;
    private Button btnAdd;
    private ArrayAdapter<Device> adapter;
    private List<Device> devices = new ArrayList<>();
    private ApiClient apiClient;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_device_list);

        apiClient = new ApiClient(this);

        listView = findViewById(R.id.list_devices);
        tvEmpty = findViewById(R.id.tv_empty);
        btnAdd = findViewById(R.id.btn_add);

        adapter = new ArrayAdapter<Device>(this, android.R.layout.simple_list_item_2, devices) {
            @NonNull
            @Override
            public View getView(int position, View convertView, @NonNull android.view.ViewGroup parent) {
                View view = super.getView(position, convertView, parent);
                Device d = getItem(position);
                if (view instanceof android.widget.TextView) {
                    ((android.widget.TextView) view).setText(d.name + "\n" + (d.host.isEmpty() ? "No host" : d.host + ":" + d.port));
                }
                return view;
            }
        };
        listView.setAdapter(adapter);

        listView.setOnItemClickListener((parent, view, position, id) -> {
            Device d = devices.get(position);
            Intent intent = new Intent(this, DeviceEditActivity.class);
            intent.putExtra("device_id", d.id);
            startActivity(intent);
        });

        btnAdd.setOnClickListener(v -> {
            Intent intent = new Intent(this, DeviceEditActivity.class);
            intent.putExtra("device_id", "");
            startActivity(intent);
        });

        loadDevices();
    }

    @Override
    protected void onResume() {
        super.onResume();
        loadDevices();
    }

    private void loadDevices() {
        apiClient.getDevices(new ApiClient.Callback<List<Device>>() {
            @Override
            public void onSuccess(List<Device> result) {
                devices.clear();
                devices.addAll(result);
                adapter.notifyDataSetChanged();
                tvEmpty.setVisibility(devices.isEmpty() ? View.VISIBLE : View.GONE);
            }

            @Override
            public void onError(String error) {
                Toast.makeText(DeviceListActivity.this, "Failed: " + error, Toast.LENGTH_SHORT).show();
            }
        });
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        getMenuInflater().inflate(R.menu.menu_device_list, menu);
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(@NonNull MenuItem item) {
        if (item.getItemId() == R.id.action_refresh) {
            loadDevices();
            return true;
        }
        return super.onOptionsItemSelected(item);
    }
}
