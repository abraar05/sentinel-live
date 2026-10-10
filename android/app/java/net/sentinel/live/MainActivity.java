package net.sentinel.live;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * Native shell for Sentinel Live.
 *
 * The heavy lifting is the gateway (Python/DVRIP) and the PWA it serves. This
 * activity exists so the app launches like any other Android app: full screen,
 * screen kept awake, and a gateway address you can set on first run.
 *
 * The gateway normally runs on a machine with LAN access to the NVRs - see
 * the README section on P2P cloud ids.
 */
public class MainActivity extends Activity {

    private static final String PREFS = "sentinel";
    private static final String KEY_URL = "gateway";
    private static final String DEFAULT_URL = "http://127.0.0.1:8080/";
    private static final int BG = 0xFF080B10;

    private WebView web;
    private View errorView;
    private String url;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        setTheme(R.style.AppTheme);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(BG);

        web = new WebView(this);
        web.setBackgroundColor(BG);
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        errorView = buildErrorView();
        errorView.setVisibility(View.GONE);
        root.addView(errorView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        setContentView(root);

        configureWeb();
        web.setOnLongClickListener(new View.OnLongClickListener() {
            @Override
            public boolean onLongClick(View v) {
                promptForGateway();
                return true;
            }
        });

        url = savedGateway();
        if (url == null) {
            promptForGateway();
        } else {
            load();
        }
    }

    // ------------------------------------------------------------- webview

    private void configureWeb() {
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        // The PWA declares width=device-width, so let the viewport meta rule.
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setDisplayZoomControls(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        // A shell has no business reading the local filesystem.
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);

        web.setOverScrollMode(View.OVER_SCROLL_NEVER);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest req) {
                return false;
            }

            @Override
            public void onPageFinished(WebView v, String pageUrl) {
                if (errorView.getVisibility() == View.VISIBLE) {
                    errorView.setVisibility(View.GONE);
                    v.setVisibility(View.VISIBLE);
                }
            }

            @Override
            @SuppressWarnings("deprecation")
            public void onReceivedError(WebView v, int code, String desc, String failing) {
                showError(code, desc);
            }

            @Override
            public void onReceivedError(WebView v, WebResourceRequest req,
                                        WebResourceError err) {
                if (Build.VERSION.SDK_INT < 23 || !req.isForMainFrame()) {
                    return;
                }
                showError(err.getErrorCode(),
                        err.getDescription() == null ? "connection failed"
                                : err.getDescription().toString());
            }
        });
    }

    private void load() {
        errorView.setVisibility(View.GONE);
        web.setVisibility(View.VISIBLE);
        web.loadUrl(url);
    }

    private void showError(int code, String detail) {
        if (isFinishing()) {
            return;
        }
        ((TextView) errorView.findViewWithTag("detail")).setText(
                code + " " + detail + "\n\n" + url);
        errorView.setVisibility(View.VISIBLE);
        web.setVisibility(View.GONE);
    }

    // ------------------------------------------------------------ chrome

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            goImmersive();
        }
    }

    /** Edge to edge: a CCTV wall has no business showing system bars. */
    private void goImmersive() {
        if (Build.VERSION.SDK_INT >= 30) {
            getWindow().setDecorFitsSystemWindows(false);
            android.view.WindowInsetsController c =
                    getWindow().getInsetsController();
            if (c != null) {
                c.hide(android.view.WindowInsets.Type.systemBars());
                c.setSystemBarsBehavior(
                        android.view.WindowInsetsController
                                .BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
            return;
        }
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_FULLSCREEN);
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        if (web.canGoBack()) {
            web.goBack();
            return;
        }
        super.onBackPressed();
    }

    // ----------------------------------------------------------- settings

    private String savedGateway() {
        SharedPreferences p = getSharedPreferences(PREFS, MODE_PRIVATE);
        return p.getString(KEY_URL, null);
    }

    private void storeGateway(String value) {
        getSharedPreferences(PREFS, MODE_PRIVATE)
                .edit().putString(KEY_URL, value).apply();
    }

    /** Accepts "192.168.1.5:8080" as readily as a full URL. */
    static String normalise(String raw) {
        String v = raw == null ? "" : raw.trim();
        if (v.isEmpty()) {
            return DEFAULT_URL;
        }
        if (!v.startsWith("http://") && !v.startsWith("https://")) {
            v = "http://" + v;
        }
        return v.endsWith("/") ? v : v + "/";
    }

    private void promptForGateway() {
        final EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setTextColor(Color.WHITE);
        input.setHint(DEFAULT_URL);
        input.setText(savedGateway() != null ? savedGateway() : "");
        int pad = Ui.dp(this, 20);
        input.setPadding(pad, Ui.dp(this, 12), pad, Ui.dp(this, 12));

        LinearLayout body = Ui.column(this);
        body.setPadding(pad, Ui.dp(this, 8), pad, 0);
        TextView hint = Ui.text(this,
                "Where the Sentinel gateway is running. Same phone: 127.0.0.1. "
                        + "Otherwise the machine's LAN address.", 13);
        hint.setTextColor(Ui.MUTED);
        body.addView(hint, Ui.matchWrap());
        body.addView(input, Ui.matchWrap());

        AlertDialog dlg = new AlertDialog.Builder(this)
                .setTitle("Sentinel gateway")
                .setView(body)
                .setPositiveButton("Connect", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int w) {
                        url = normalise(input.getText().toString());
                        storeGateway(url);
                        load();
                    }
                })
                .setNegativeButton("Cancel", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int w) {
                        if (savedGateway() == null) {
                            finish();
                        }
                    }
                })
                .setCancelable(savedGateway() != null)
                .create();

        dlg.setOnShowListener(new DialogInterface.OnShowListener() {
            @Override
            public void onShow(DialogInterface d) {
                goImmersive();
            }
        });
        dlg.show();
    }

    // ------------------------------------------------------- error screen

    private View buildErrorView() {
        LinearLayout col = Ui.column(this);
        col.setBackgroundColor(BG);
        col.setGravity(Gravity.CENTER);
        col.setPadding(Ui.dp(this, 28), Ui.dp(this, 28), Ui.dp(this, 28), Ui.dp(this, 28));

        TextView glyph = Ui.text(this, "⛌", 40);
        glyph.setTextColor(Ui.MUTED);
        col.addView(glyph, Ui.center());
        col.addView(Ui.spacer(this, 10));

        TextView title = Ui.text(this, "Can't reach the gateway", 19);
        title.setGravity(Gravity.CENTER);
        col.addView(title, Ui.center());
        col.addView(Ui.spacer(this, 6));

        TextView detail = Ui.text(this, "", 13);
        detail.setTextColor(Ui.MUTED);
        detail.setGravity(Gravity.CENTER);
        detail.setTag("detail");
        col.addView(detail, Ui.matchWrap());
        col.addView(Ui.spacer(this, 20));

        Button retry = Ui.button(this, "Retry", true);
        retry.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                load();
            }
        });
        col.addView(retry, Ui.center());
        col.addView(Ui.spacer(this, 10));

        Button settings = Ui.button(this, "Change gateway", false);
        settings.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                promptForGateway();
            }
        });
        col.addView(settings, Ui.center());
        return col;
    }
}